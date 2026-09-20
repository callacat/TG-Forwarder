# -*- coding: utf-8 -*-
"""TG-Forwarder v3 核心层包（账号生命周期/看门狗/转发引擎）。

导出核心类，供 main.py 与测试直接引用：
    from tg_forwarder.core import AccountManager, Supervisor, Forwarder
"""
from tg_forwarder.core.accounts import AccountManager, ProxyFallback
from tg_forwarder.core.supervision import Supervisor
from tg_forwarder.core.forwarder import Forwarder

__all__ = ["AccountManager", "ProxyFallback", "Supervisor", "Forwarder"]
