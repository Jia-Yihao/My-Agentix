"""LangGraph adapter 层（M2 迁入；``langgraph`` 导入白名单见 p66 守卫 #11）。

刻意零 import 的包 ``__init__``：lg_builder 与 lg_supervisor 为模块级互引环，
任何 eager 汇总导出都会在特定加载顺序下炸半初始化。
（2026-10-29 M3-5b 返工更正：上面「模块级互引环」已不成立——lg_builder 侧
本就是函数体内延迟 import，lg_supervisor 侧顶层 import lg_builder 也已下移到
``build_supervisor_graph()`` 调用体内，两模块之间不再有模块级互引。本文件
「刻意零 import」维持，但理由换了一条且更强：M3-5b 起 lg_builder 的 import
期要求装配根已加载，eager 汇总导出会把这份欠账转嫁给每一个 import 本包的
进程，比半初始化更难查。）
"""
