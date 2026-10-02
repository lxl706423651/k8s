# 方法依据与可以支持的结论

PDF 引用的工作支持受控干扰实验和性能画像这一方法，并不能直接证明本项目的 CPU/Memory 两维输入足够。

- [Paragon，作者版论文](https://csl.stanford.edu/~christos/publications/2013.paragon.asplos.pdf)：联合考虑硬件异构性与共置干扰；本项目因此先分别标定 VM 类型，再验证能否共享归一化曲线。
- [Quasar 与 iBench，作者研究页](https://www.csl.cornell.edu/~delimitrou/Research.html)：性能目标与多种共享资源的干扰敏感性是相关但不同的信息；不能把资源预算直接等同于应用性能保证。
- [SLOMO，SIGCOMM 2020 作者版论文](https://users.ece.cmu.edu/~vsekar/assets/pdf/sigcomm20_slomo.pdf)：区分 workload 对共享资源施加的压力与对干扰的敏感性。本项目只预测固定 VM canary 的能力，额外任务必须通过不同实现的预算一致性测试，并保留不能由两维解释的反例。
- [Bubble-Up 方法，作者网站论文版本](https://www.jasonmars.org/wp-content/uploads/2020/12/Increasing_Utilization_in_Modern_Warehouse-Scale_C.pdf)：用受控压力观察退化曲线的思路与本项目的离线扫描一致。该链接为作者网站托管版本，不能混同为 PDF 中列出的 MICRO 2011 原始版本。

上述解释是本项目对方法适用性的判断；不是这些论文对本实现的验证。尤其不能用引用数量代替新模型的独立测试、误差报告和部署对照。

## 不确定性余量的统计表述

拟合程序以随机化区块为校准单位，取每块最大乐观残差形成一侧余量。5 个区块不足以给出有限的 95% split-conformal 排名（ceil((5+1)×0.95)=6），而且训练/校准/测试压力点来自不同的设计分布，不满足直接套用交换性保证的前提。因此程序标记 `coverageCertified=false`；使用经验最大残差不能写成“95% 保证安全”。实际部署的违约率和独立区块置信界仍必须单独测量。
