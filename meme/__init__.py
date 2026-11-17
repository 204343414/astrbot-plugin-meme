"""梗图插件的业务层。

这个包里一个 ``astrbot`` 都不 import —— 审核判定、入库、随机挑选、
投递降级全是不依赖运行时框架的纯逻辑，因此可以在没有 AstrBot 的机器上
直接跑单元测试。``main.py`` 才是唯一碰 AstrBot 的地方：它把 provider
包成 adapter，把 event 包成 sender，然后交给这里。
"""
