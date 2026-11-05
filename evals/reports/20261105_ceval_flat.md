# 多 agent 系统性能报告 · qwen3-8b 扁平拓扑

- 题目数：**156**
- MCQ 正确率：**0.7097**（110/155 可判分；抽不出字母 1 题）
- 抽取方式分布：`letter` 155 · `none` 1
- 路由准确率（期望 general-query）：1

## 延迟与模型调用

- 单题墙钟：p50 **5923 ms** · p90 **6931 ms** · 均值 6214 ms · 最大 1.533e+04 ms（n=156）
- 每题 LLM 步数：均值 **0** · 分布 `0` 156

## 多 agent 出口契约

- 不适用（本次录制没有 supervisor 元数据 ⇒ 跑的是扁平拓扑）

## 逐学科正确率

| 学科 | 题数 | 正确 | 正确率 |
|---|---|---|---|
| accountant | 3 | 2 | 0.6667 |
| advanced_mathematics | 3 | 1 | 0.3333 |
| art_studies | 3 | 2 | 0.6667 |
| basic_medicine | 3 | 2 | 0.6667 |
| business_administration | 3 | 2 | 0.6667 |
| chinese_language_and_literature | 3 | 2 | 0.6667 |
| civil_servant | 3 | 3 | 1 |
| clinical_medicine | 3 | 2 | 0.6667 |
| college_chemistry | 3 | 1 | 0.3333 |
| college_economics | 3 | 2 | 0.6667 |
| college_physics | 3 | 3 | 1 |
| college_programming | 3 | 2 | 0.6667 |
| computer_architecture | 3 | 2 | 0.6667 |
| computer_network | 3 | 3 | 1 |
| discrete_mathematics | 3 | 2 | 0.6667 |
| education_science | 3 | 3 | 1 |
| electrical_engineer | 3 | 1 | 0.3333 |
| environmental_impact_assessment_engineer | 3 | 1 | 0.3333 |
| fire_engineer | 3 | 2 | 0.6667 |
| high_school_biology | 3 | 2 | 0.6667 |
| high_school_chemistry | 3 | 2 | 0.6667 |
| high_school_chinese | 3 | 1 | 0.3333 |
| high_school_geography | 3 | 3 | 1 |
| high_school_history | 3 | 2 | 0.6667 |
| high_school_mathematics | 3 | 3 | 1 |
| high_school_physics | 3 | 1 | 0.3333 |
| high_school_politics | 3 | 2 | 0.6667 |
| ideological_and_moral_cultivation | 3 | 2 | 0.6667 |
| law | 3 | 2 | 0.6667 |
| legal_professional | 3 | 2 | 0.6667 |
| logic | 3 | 3 | 1 |
| mao_zedong_thought | 3 | 2 | 0.6667 |
| marxism | 3 | 3 | 1 |
| metrology_engineer | 3 | 2 | 0.6667 |
| middle_school_biology | 3 | 3 | 1 |
| middle_school_chemistry | 3 | 3 | 1 |
| middle_school_geography | 3 | 3 | 1 |
| middle_school_history | 3 | 3 | 1 |
| middle_school_mathematics | 3 | 0 | 0 |
| middle_school_physics | 3 | 3 | 1 |
| middle_school_politics | 3 | 3 | 1 |
| modern_chinese_history | 3 | 3 | 1 |
| operating_system | 3 | 3 | 1 |
| physician | 3 | 1 | 0.3333 |
| plant_protection | 3 | 1 | 0.3333 |
| probability_and_statistics | 3 | 1 | 0.3333 |
| professional_tour_guide | 3 | 3 | 1 |
| sports_science | 3 | 2 | 0.6667 |
| tax_accountant | 3 | 1 | 0.3333 |
| teacher_qualification | 3 | 3 | 1 |
| urban_and_rural_planner | 3 | 1 | 0.3333 |
| veterinary_medicine | 3 | 3 | 1 |

