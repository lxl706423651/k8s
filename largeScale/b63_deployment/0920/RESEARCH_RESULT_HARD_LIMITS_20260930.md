# 2026-09-30：1078节点硬CPU/内存预算验证

本轮冻结原KaHyPar-E成本55与后处理成本53映射，在每容器requests=limits条件下完成实际部署、路由验证和清理。分区输入仍仅主机CPU/内存和拓扑；任何运行观测均不反馈至分区或配额。上一轮requests-only成功不能代替这次硬限制实验。

## 结果

|组别|跨度S|接纳|协议检查/失败|FIB AS代表|ICMP收到/发送|发生CPU节流的容器|OOM事件|
|---|---:|---:|---:|---:|---:|---:|---:|
|baseline|55|1078|1078/0|82|10482/10482|1078|0|
|candidate|53|1078|1078/0|82|10482/10482|1078|0|

两组每容器均为55m CPU、105115834字节内存；逐容器读取实际cpu.max和memory.max，允许内存向下按页对齐。启动前和结束后cgroup身份一致，CPU计数单调，零重启。所有运行清单除了主机亲和性之外一致，探测集合一致。实际父接口仍为1120/1118，不能把2个接口差异说成明显性能提升。

## 设计与证据

预登记design.json冻结顺序baseline→candidate、manifest散列及失败判据；runtimeRegistration.json冻结执行时脚本。每臂前全VM三次load1<10，启动与FIB验证前load1<50。采用原镜像和Kernel政策（扫描6000+节点哈希mod121秒），没有测量节点、重新构建镜像或改变VM配置。

collectHardCgroups.py两次读取1078个容器的cpu.max/cpu.stat/memory.max/memory.events/memory.current，实际限制核验通过。CPU节流说明限制确实影响了执行；统计包含验证命令自身开销，累计节流秒数不等于墙钟停顿或应用慢速比例。结束时内存占用不是峰值，不能据此减小额度。

全量协议覆盖1078节点；FIB仍为82 AS最小编号代表的网络数/路由条目数判据，不是全前缀下一跳等价证明。ICMP为相同LAN双向星形探测，非端到端吞吐或业务QoS。路由网络计数差异详见summary.json，计数相等也不证明全部路由内容一致。

复用helper中的admission.json/complete.json部分scope文案继承上一轮requests-only描述；实际本轮政策以design.json、deployment.yaml及全量cgroup原始记录为准，独立审计同时检查requests和limits，未篡改历史/运行记录。上一轮缓存的镜像仓库别名允许不同，但Pod指定引用和实际SHA256均核验。

## 研究判断

在该1078节点静态拓扑与固定预留政策下，共同额度不仅可被调度器接纳，也能在CPU/内存硬上限生效时完成路由流程。它为模型提供了比requests-only更强的实例有效性证据；不证明数学最大额度、任意负载保证或所有拓扑有效。

本轮是一组固定顺序对照，不作统计性能胜负判断。四VM共用物理宿主机；worker16/24/32 vCPU与32/32/64 GiB，管理VM8 vCPU/16 GiB单列。尚未覆盖6000秒周期扫描、路由故障扰动及高流量场景。未新增Crescent系统运行，不能宣称全面超过Crescent。

下一步应保持输入与模型不变，预登记确定性BGP会话撤销/恢复或链路故障场景，在同硬配额下交替顺序重复运行，测量可达性恢复与CPU节流；先在小规模核验扰动脚本。评估场景只用于验证，不作为分区需求输入。

实验namespace已删除，四节点Ready且无残留BIRD，VM和镜像缓存保留。命令、原始记录与复现说明见[实验目录](results/topology-hardlimits-20260930/README.md)，对照前轮见[requests-only报告](RESEARCH_RESULT_SYSTEM_20260929.md)。
