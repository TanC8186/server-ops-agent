# AutoDL 官方知识库来源清单

这些页面来自 AutoDL 官方帮助文档或官方 API 文档，作为 AutoDL SSH 目标的 RAG 语料候选来源。抓取时必须保存原文 hash、抓取时间、source URL、版本范围和 license 状态；未完成许可核对前只能标记为内部验证语料。

| ID | 官方页面 | 适用主题 | 建议 metadata |
|---|---|---|---|
| AD-ENV | https://www.autodl.com/docs/env/ | 容器实例、系统盘、数据盘、/root/autodl-tmp、Docker 限制 | product=autodl, os_family=linux, target_kind=ssh |
| AD-QUICK | https://www.autodl.com/docs/quick_start/ | 实例创建、镜像、JupyterLab、终端训练 | product=autodl, target_kind=ssh |
| AD-IMAGE | https://www.autodl.com/docs/image/ | 保存镜像、更换镜像、系统盘与数据盘关系 | product=autodl, target_kind=ssh |
| AD-DATA | https://www.autodl.com/docs/instance_data/ | 实例释放、数据保留、磁盘和备份边界 | product=autodl, target_kind=ssh |
| AD-GPU | https://api.autodl.com/docs/gpu/ | GPU 选型、CPU/内存配比、CUDA 版本注意事项 | product=autodl, target_kind=ssh |
| AD-SCP | https://www.autodl.com/docs/scp/ | SSH/SCP 端口格式、文件上传和下载 | product=autodl, target_kind=ssh |
| AD-QA | https://www.autodl.com/docs/qa/ | SSH 断开、screen/tmux、实例释放、内存被 kill | product=autodl, target_kind=ssh |
| AD-FS | https://www.autodl.com/docs/fs/ | 文件存储 /root/autodl-fs、本地数据盘与共享存储取舍 | product=autodl, target_kind=ssh |
| AD-PRO-API | https://www.autodl.com/docs/instance_pro_api/ | Pro 实例 API、SSH 字段、磁盘和 GPU 状态字段 | product=autodl-pro, target_kind=ssh |

## 入库规则

1. 只抓取表中明确 URL，不递归站点，不把用户输入的 URL 直接交给抓取器。
2. 保存 raw HTML、normalized Markdown、notices 和 manifest；记录 HTTP 状态、content type、字节数和 sha256。
3. AutoDL 页面中的密码、token、实例连接信息和 API 示例值全部视为敏感内容；入库前检查并脱敏，不把用户实例的真实值写入语料。
4. 官方文档中的命令属于知识证据，不是本 Agent 的自动执行授权；执行仍受 SSH 工具白名单和参数校验约束。
5. 与 Ubuntu、NVIDIA、Docker、vLLM 文档分开记录 source_family，检索时通过 product、os_family、target_kind、version 过滤。
6. AutoDL 版本和产品页面发生变化时创建新 revision，不覆盖旧文档；答案引用必须显示 revision 和来源 URL。

## 当前边界

- AutoDL 官方文档可以支撑平台使用、存储、SSH、GPU 资源和实例生命周期说明。
- 它不能支撑当前实例的真实 CPU、内存、磁盘、GPU 使用率；这些必须来自 SSH 只读观测。
- 不能因为官方文档提到某条命令，就声称该命令已经在当前 AutoDL 实例执行成功。
