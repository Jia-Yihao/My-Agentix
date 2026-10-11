"""LangGraph adapter 层（M2 迁入；``langgraph`` 导入白名单见 p66 守卫 #11）。

刻意零 import 的包 ``__init__``：lg_builder 与 lg_supervisor 为模块级互引环，
任何 eager 汇总导出都会在特定加载顺序下炸半初始化。
"""
