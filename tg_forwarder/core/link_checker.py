# -*- coding: utf-8 -*-
"""死链检测（v2 link_checker.py 全量迁移，log/edit/delete 三模式 + schedule）。"""
import re
from typing import List

import httpx
from loguru import logger
from telethon import TelegramClient
from telethon.errors import RPCError

from tg_forwarder.storage.db import Database

# 网盘域名（v2 对齐）
NET_DISK_DOMAINS = [
    "pan.quark.cn",
    "aliyundrive.com",
    "alipan.com",
    "115.com",
    "pan.baidu.com",
    "cloud.189.cn",
    "drive.uc.cn",
]

URL_PATTERN = r"https://?[^\s]+"


def extract_links(message_text: str, domains: List[str] = None) -> List[str]:
    """从消息文本提取网盘链接（纯函数）。"""
    if not message_text:
        return []
    urls = re.findall(URL_PATTERN, message_text)
    links = [u for u in urls if any(d in u for d in (domains or NET_DISK_DOMAINS))]
    return list(set(links))


class LinkChecker:
    """死链检测器（对齐 v2 行为）。"""

    def __init__(self, db: Database, client: TelegramClient, config):
        self.db = db
        self.client = client
        self.config = config
        self.checker_config = config.link_checker
        self.target_channel_identifier = config.settings.default_target
        self.target_channel_id = None

    async def _check_link_validity(self, url: str) -> bool:
        """HEAD 检查；网络错误视为有效（v2 对齐）。"""
        try:
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=True
            ) as client:
                response = await client.head(url, headers={"User-Agent": "Mozilla/5.0"})
                return response.status_code < 400
        except httpx.RequestError as e:
            logger.warning(f"检测链接 {url} 时发生网络错误: {e}")
            return True
        except Exception as e:
            logger.error(f"检测链接 {url} 时发生未知错误: {e}")
            return True

    async def run(self) -> None:
        """主逻辑：扫描频道 → 检测 → 按模式处置。"""
        logger.info("--- 启动失效链接检测器 ---")
        if not self.checker_config or not self.checker_config.enabled:
            logger.error("Link checker 未在配置中启用。")
            return

        if not self.target_channel_id:
            try:
                entity = await self.client.get_entity(self.target_channel_identifier)
                self.target_channel_id = entity.id
            except Exception as e:
                logger.error(
                    f"无法解析链接检测器的目标频道: "
                    f"{self.target_channel_identifier} - {e}"
                )
                return

        logger.info(f"检测模式: {self.checker_config.mode}")
        last_processed_id = await self.db.get_link_checker_progress()
        logger.info(f"从消息 ID {last_processed_id} 开始扫描频道...")

        new_links_found = 0
        try:
            async for message in self.client.iter_messages(
                self.target_channel_id, min_id=last_processed_id
            ):
                if not message.text:
                    continue
                links = extract_links(message.text)
                if links:
                    for link in links:
                        await self.db.add_pending_link(link, message.id)
                        new_links_found += 1
                last_processed_id = max(last_processed_id, message.id)

            await self.db.set_link_checker_progress(last_processed_id)
            logger.info(f"频道扫描完成，发现 {new_links_found} 个新链接（或已存在）。")
        except Exception as e:
            logger.error(f"扫描频道 {self.target_channel_id} 失败: {e}")

        links_to_check = await self.db.get_links_to_check()
        logger.info(f"总共有 {len(links_to_check)} 个链接需要检测...")

        invalid_messages = {}
        for (link, msg_id) in links_to_check:
            is_valid = await self._check_link_validity(link)
            if is_valid:
                await self.db.update_link_status(link, "valid")
            else:
                await self.db.update_link_status(link, "invalid")
                logger.warning(f"检测到失效链接: {link} (Message ID: {msg_id})")
                invalid_messages.setdefault(msg_id, []).append(link)

        mode = self.checker_config.mode
        protect_doms = self.checker_config.delete_protect_domains or []

        if mode == "log":
            logger.info(f"检测完成 (日志模式)：{len(invalid_messages)} 条含失效链接。")
            logger.info("--- 失效链接检测器运行完毕 ---")
            return

        # F3 分域风控：先拆分可删与保护域链接（保护域只标记不自动删除）
        delete_candidates = {}
        mark_only = {}
        for msg_id, links in invalid_messages.items():
            pr = [l for l in links if any(d in l for d in protect_doms)]
            di = [l for l in links if l not in pr]
            if pr:
                mark_only[msg_id] = pr
            if di:
                delete_candidates[msg_id] = di

        # F3 二次复核：删除类模式删除前对候选链接逐个再验一次，防网络瞬断误删
        if mode in ("delete", "delete_marked") and self.checker_config.recheck_before_delete:
            rechecked = {}
            for msg_id, links in delete_candidates.items():
                still_bad = [l for l in links if not await self._check_link_validity(l)]
                if still_bad:
                    rechecked[msg_id] = still_bad
            skipped = sum(len(v) for v in delete_candidates.values()) - sum(
                len(v) for v in rechecked.values()
            )
            if skipped:
                logger.info(f"F3 二次复核：{skipped} 个链接复核后仍有效，跳过删除（防误删）。")
            delete_candidates = rechecked

        if mode == "edit":
            logger.info("检测完成 (仅标记模式)。")
            await self._edit_invalid(invalid_messages)
        elif mode == "delete":
            if delete_candidates:
                await self._delete_invalid(delete_candidates)
            if mark_only:
                logger.info(
                    f"F3 保护域（{protect_doms}）失效链接仅标记不自动删。"
                )
                await self._edit_invalid(mark_only)
        elif mode == "delete_marked":
            # 只删已标记过的消息（防误删首次出现失效）；首次发现的先标记等下轮
            to_delete = {}
            to_mark = {}
            for msg_id, links in delete_candidates.items():
                if await self._is_marked(msg_id):
                    to_delete[msg_id] = links
                else:
                    to_mark[msg_id] = links
            if to_delete:
                await self._delete_invalid(to_delete)
            if to_mark or mark_only:
                await self._edit_invalid({**to_mark, **mark_only})

        logger.info("--- 失效链接检测器运行完毕 ---")

    async def _is_marked(self, msg_id) -> bool:
        """快照消息是否已被标记过（delete_marked 档判据）。"""
        try:
            message = await self.client.get_messages(self.target_channel_id, ids=msg_id)
            return bool(message and "[链接已失效]" in (message.text or ""))
        except Exception as e:
            logger.error(f"读取消息 {msg_id} 判定标记状态失败: {e}")
            return False

    async def _edit_invalid(self, invalid_messages) -> None:
        logger.info("正在编辑包含失效链接的消息...")
        for msg_id, links in invalid_messages.items():
            try:
                message = await self.client.get_messages(
                    self.target_channel_id, ids=msg_id
                )
                if not message or not message.text:
                    continue
                if "[链接已失效]" in message.text:
                    logger.debug(f"消息 {msg_id} 已被标记，跳过。")
                    continue
                new_text = message.text
                for link in links:
                    new_text = new_text.replace(link, f"{link} [链接已失效]")
                await self.client.edit_message(
                    self.target_channel_id, msg_id, new_text
                )
                logger.info(f"已编辑消息 {msg_id}")
            except Exception as e:
                logger.error(f"编辑消息 {msg_id} 失败: {e}")

    async def _delete_invalid(self, invalid_messages) -> None:
        logger.info("正在删除包含失效链接的消息...")
        msg_ids = list(invalid_messages.keys())
        try:
            await self.client.delete_messages(self.target_channel_id, msg_ids)
            logger.info(f"已删除 {len(msg_ids)} 条消息。")
        except RPCError as e:
            logger.error(f"批量删除消息失败: {e}")
