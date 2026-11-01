# 第三轮完成效果复核与下一阶段建议

日期：2026-10-27。对象：当前工作区，最新本地提交为 ed13769，工作区当前无未提交业务代码改动。本报告把自动化、真实依赖和未验证边界分开。

## 1. 验收结论

**本轮 G1/G2 的核心工程目标基本达成，可以作为可演示版本；仍不应描述为已接入生产或支持完整企业多租户。**

本次复跑：

- 后端：717 passed、1 skipped、2 warnings
- 前端：53 passed，build、lint、tsc 通过
- 三个离线反例探针：全部 bug_reproduced=false
- 真实 Milvus：v2.5.10，11 篇 core 文档、951 个 core chunks，384 维 COSINE；更新/回滚/部分写入失败/描述符错配/停机恢复均有记录
- 真实 Docker：镜像 sha256:7788efe302394...，18.9GB；Compose 全栈 healthy、401/200 鉴权、SIGTERM、MCP 子进程退出、命名卷重建均有记录
- 真实 LLM：一次完整 cloud_multi，Supervisor→metrics/logs→Reviewer，completed/sufficient
- 当前浏览器构建：1440/1100/1024/768/390/375 五档无横向溢出，知识库面板连真实 Milvus，窄屏替代入口和 aria-live 可见

证据的版本边界也要保留：Docker 验收文件标记的构建前工作区是 266a54c，而当前最新提交是 ed13769；其间包含真实云模型解析相关代码变化。因此该镜像证明 Docker/Compose 流程有效，但不能直接作为最新提交的镜像 digest。发布前必须从当前干净提交重新 build、force-recreate 并生成新 digest。

证据文件：

- [真实 Milvus 验收](MILVUS_ACCEPTANCE_2026-10-27.md)
- [Docker/Compose 验收](DOCKER_ACCEPTANCE_2026-10-27.md)
- [真实 LLM 链路验证](CLOUD_SMOKE_2026-10-27.md)
- [当前浏览器旅程](BROWSER_JOURNEY_2026-10-27.md)
- [盲测与语料审计](BLIND_EVAL_2026-10-27.md)

## 2. 最重要的效果判断：检索好，回答门控偏保守

真实 Milvus 盲测使用 77 条题：

| 指标 | 结果 |
|---|---:|
| 可答题 | 58 |
| 无答案题 | 19 |
| Recall@1 | 0.8793 |
| Recall@3 | 0.9828 |
| MRR | 0.9282 |
| nDCG@5 | 0.9446 |
| 无答案误答 | 0/19 |
| dev 拟合阈值 | 0.6315 |
| 可答题实际放行 | 29/58 = 50.0% |

这说明当前主要瓶颈不是向量检索，而是 no-answer gate：可答题最低 top1 分数 0.3515，无答案最高分数 0.5818，分布有重叠。将阈值直接调低会增加误答；继续维持 0.6315 会大量漏答。该问题应作为下一阶段的评测与产品策略问题处理，不能写成“RAG 准确率 98.3%”。

另一次显式 threshold=0 的结果全部回答但无答案误答率升为 1.0，证明单纯取消门控不是修复方案。

## 3. 仍未闭环的边界

### 3.1 真实业务工具边界

真实 LLM 链路验证使用了真实模型，但工具层是与真实 MCP payload 形态一致的进程内 fake。SSH 只读 staging 没有执行；真实腾讯云监控/日志没有接入生产数据。可以写“真实 LLM 多智能体链路”，不能写“真实服务器 SSH 已联调”或“取消能终止远端任务”。

### 3.2 浏览器任务旅程

当前构建已验证布局、知识库 ready 展示、模式刷新、窄屏 inspector、合成 IME 和 aria-live DOM。以下仍未在浏览器中完成：

- 同名双实例真实 MCP 目录下的候选点击与 target_candidate_id 绑定；
- 失败索引作业的重试按钮；
- SSE 断流续读、tab 关闭再打开后的历史一致性；
- 真实问答引用卡片；
- 真实中文输入法进程和屏幕阅读器。

### 3.3 权限与部署边界

已有 principal 归属检查，但仍是单租户模型；知识文档的 ACL、OS/产品/版本/有效期候选过滤尚未形成完整策略。trace 锁主要是进程内锁，多副本并发还没有实测。

### 3.4 语料治理

SD-02、LK-01、RH-01 仍保持 needs_review，没有擅自升级许可状态。DK-03 页面正文确实很短，不是抽取失败，但需要在 manifest 标注“源页面无示例”。滚动 latest/main 的 commit/blob pin 仍需补齐。

### 3.5 工程维护信号

当前仍能看到 Pydantic class Config 弃用警告、PyMilvus ORM/utility 风格 API 弃用警告、Ruff 顶层 lint 配置迁移警告、Node 实验性 strip-types 测试基建、Docker 镜像约 18.9GB。Compose 若直接复用同名旧镜像，可能验收到陈旧代码，因此流水线必须显式 build/force-recreate 或按 digest 部署。

## 4. 下一阶段建议

### H1：先解决 RAG 门控与生成式问答评测

1. 冻结当前 77 条 blind 集，另建 calibration/dev-2 集，禁止在 blind 上调阈值。
2. 按 zh_to_en_manual、exact_command、os_version_conflict、misleading_trap 分档校准阈值，比较全局阈值、分档阈值和 top-k 多证据门控。
3. 在固定真实 LLM、prompt 和 Milvus snapshot 上测试引用支持率、事实正确率、无答案误答率；目前 blind 只有检索指标，没有完整回答正确率。
4. 将分数、门控原因、拒答原因显示在 UI，避免用户只看到“没有答案”而不知道是无命中还是阈值拒绝。

目标是同时报告 retrieval、gate、generation 三层指标，不追求一个笼统的“RAG accuracy”。

当前两个 blind 结果文件还反映了两种策略：0.6315 阈值拒绝 29/58 条可答题，threshold=0 则误答 19/19 条无答案题。两份结果都应保留，后续只能在新的 calibration 集上选择策略，不能把任一结果单独包装成系统准确率。

### H2：补齐文档级适用范围与多租户边界

- 在候选检索前执行 principal/OS/产品/版本/有效期过滤，未授权片段不能进入 reranker 或模型。
- 设计单租户演示模式与多租户部署模式的明确开关；不要只靠 session_id 或客户端 tenant_id。
- 多副本场景把 trace、job、ACL 和 active revision 元数据迁移到共享存储，或在 README 明确单实例边界。

### H3：完成真实 MCP 与浏览器闭环

- 用 staging MCP 返回两个同名服务实例，真实点击第二个候选并验证绑定。
- 注入真实索引失败，浏览器点击 retry，确认旧 active 仍可问答。
- 人为断开 SSE，验证按 run/seq 续读；关闭 tab 后重开，确认历史和服务端会话一致。
- 真实问答运行至少一个引用卡片旅程，核对 citation ID、revision、locator、snippet。
- 用真实中文输入法和 NVDA/等效屏幕阅读器补齐可访问性记录。

### H4：稳定部署与依赖升级

- CI 固定 docker build --pull --no-cache 或输出镜像 digest，禁止默认复用同名旧镜像。
- 处理 Pydantic、PyMilvus、Ruff 的弃用警告；每项升级都要重跑真实 Milvus acceptance。
- 拆分模型/依赖层，评估 18.9GB 镜像是否可降到可接受范围；不要牺牲本地离线能力换体积数字。
- 对 retention 后台任务、abandoned 状态和跨进程 trace 锁追加长时间/并发测试。

## 5. 可对外陈述的范围

可以写：

> 构建支持本地 QLoRA 小模型与在线 API 多智能体编排的双模式运维 Agent；实现受约束工具执行、目标绑定、证据核验、版本化 RAG 作业和可追溯引用。基于真实 Milvus v2.5.10 完成 11 篇运维手册、951 个核心分片的导入/更新/回滚/失败清理验证；77 条独立盲测上 Recall@3=0.9828、MRR=0.9282、nDCG@5=0.9446，并单独报告门控误拒情况。真实 LLM 已跑通 Supervisor→metrics/logs→Reviewer，Docker 镜像、鉴权、子进程监督和卷重建有验收记录。

不要写“RAG 准确率 98.3%”“支持完整多租户权限”“SSH 已联调”“取消可以终止远端生成”或“已在生产环境部署”。
