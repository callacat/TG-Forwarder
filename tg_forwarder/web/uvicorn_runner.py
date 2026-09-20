# -*- coding: utf-8 -*-
"""TG-Forwarder v3 uvicorn 运行器薄封装。

供 main.py 与其他 task 共同 gather：返回未启动的 uvicorn.Server 实例，
由调用方决定 serve() 时机。
"""
import uvicorn


def run_server(app, host: str = "0.0.0.0", port: int = 8080):
    """构建 uvicorn.Server（log_config=None, access_log=False），返回 server 实例。

    与 v2 ultimate_forwarder.py 的启动方式一致：
        server = uvicorn.Server(uvicorn.Config(app, host=..., port=..., log_config=None, access_log=False))
    调用方自行 ``await server.serve()`` 或加入 asyncio.gather。
    """
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
    )
    return uvicorn.Server(config)
