# 技能包（Skill）

本目录把开发过程中沉淀的**可复用经验**显式提炼为通用 Skill，供其他队伍直接复用到自己的题目上。两个 Skill 均不绑定本题的算法、图片或项目路径——换一个题目、换一块板卡（带 AXI DMA 的 PYNQ Overlay）仍可直接使用。

| Skill | 用途 | 适用范围 | 详见 |
|---|---|---|---|
| `dma_test/` | DMA 发送/接收链路通用检查 | 任意带 AXI DMA 的 PYNQ Overlay；内核从 MM2S 读流、向 S2MM 写流 | [README](dma_test/README.md) |
| `data/evidence/benchmark/` | 通用性能统计（mean/median/fps + CSV） | 任意 Python 可调用对象；PC 与板端均可 | README |

两个 Skill 均已在本项目板端实测验证，验证记录见 skill_validation_results.md。

## 如何复用

把对应目录复制到你的工程（或 clone 本仓库），按各自 README 中的「调用示例」引入即可。每个 README 都写清了适用范围、使用方法、失效条件与排查顺序。

