# 安全、凭据与对外口径审查（2026-10-30）

> 本文件只记录**状态、路径、计数**，不含任何凭据值。

## 1. 仓库与构建上下文

| 检查 | 结果 |
|---|---|
| Git 跟踪的敏感文件（`.env*` / `*.pem` / `*.key` / `id_rsa` / `*.ppk`） | **0 个** |
| 唯一匹配项 | `tests/test_token_chunking.py`（文件名含 "token"，非凭据） |
| AutoDL 私钥位置 | `E:/On_Call_Agent/.tmp_g8_keys/`——**仓库之外** |
| 该目录的忽略规则 | `.gitignore:129` 的 `.tmp*/` 覆盖 |
| `.dockerignore` 敏感模式 | `**/*.pem` / `**/*.key` / `**/*.ppk` / `**/id_rsa*` / `**/.env*`（`**/` 前缀，覆盖嵌套路径） |
| 镜像内是否含私钥 | 已验证：新构建镜像 `find /app -name "*.pem"` 只命中 site-packages 的 CA 证书 |

## 2. 本轮新增的凭据面

| 项 | 状态 |
|---|---|
| AutoDL SSH 私钥（专用 RSA，本轮生成） | 存于仓库外 `.tmp_g8_keys/`，容器以只读挂载 `/app/ssh_key.pem`。**不入库、不入镜像**。 |
| AutoDL 登录密码 | **已在对话中暴露一次**，未写入任何文件。已改为公钥认证，密码不再使用。**建议轮换。** |

## 3. 轮换清单

| 凭据 | 状态 | 说明 |
|---|---|---|
| AutoDL SSH 密码 | **待轮换** | 对话中暴露过；删文件不等于失效 |
| `LLM_API_KEY` | 待轮换 | 沿用 G7 清单 |
| `TENCENTCLOUD_SECRET_ID` / `_KEY` | 待轮换 | 沿用 G7 清单 |
| `servers/AgentTest.pem`（旧 RSA 私钥） | **必须轮换** | 曾真实存在于旧镜像层内 |
| 本轮生成的 AutoDL 专用私钥 | 按需 | 仅存在于本机，未外泄 |

## 4. 日志与产物

| 检查 | 结果 |
|---|---|
| `logs/` 内是否有凭据值 | 无（G7 已脱敏，本轮未新增凭据日志） |
| 报告文档内是否有密钥/密码字面量 | 无（grep `BEGIN (RSA\|OPENSSH) PRIVATE KEY` 与 password 赋值模式均 0 命中） |
| 探针脚本与会话临时文件 | 已清理（`build_*.log`、`p*.html`、`probe*.json/sse`、`tmp.*`） |
| `store.backup_before_readd.json` | 备份放在仓库外 `E:/On_Call_Agent/` |

## 5. 对外口径审查

### 5.1 已验证的事实（有证据支撑）

- 抓取并规范化 9 个 AutoDL 官方文档源，抽取 `<article>` 正文共 16 031 字符，
  写入 Milvus（9 篇文档 / 183 chunk）。
- 为知识库增加 `target_kind` / `source_kind` 元数据维度，并在真实 Milvus 上
  验证 `target_kind=cloud` 返回 0 条、`target_kind=ssh` 返回 3 条。
- 定位并修复 3 个"看起来做了、实际没生效"的缺陷：元数据字段未落盘、
  无 principal 时过滤被静默绕过、删除文档未清理已完成作业。
- 修复容器观测口径：`free` 报 1 TiB 而 cgroup 实为 2 GiB（高估 512 倍）、
  数据盘不可见、`check_disk_usage` 默认 `C:/` 在 Linux 必失败、
  `read_log_file` 伪造不存在的文件。
- 930 个后端测试通过。

### 5.2 不得作为结论的主张

- ✗ "AutoDL 官方文档已进入 RAG 且双模式对比完成"——对比**尚未执行**。
- ✗ "本地 AutoDL adapter 提升了准确率"——旧的 16 条评测存在 **16/16 输出泄漏**，
  且 Tool F1 0.766 **低于常数基线 0.800**。
- ✗ "已支持 journal 日志查询"——未实现，当前明确返回 unavailable。
- ✗ "GPU 不可用能被明确识别"——没有 GPU 工具，做不到。
- ✗ "`ssh_port` 已用于实际连接"——目录里的端口目前只是元数据，
  连接仍读全局 `SSH_PORT`。
- ✗ "多副本/多租户"——单 API Key、单进程存储。

## 6. 未完成

1. AutoDL SSH 密码轮换需持有人执行。
2. `mact-*` / `personal_design-*` 资源本轮未触碰，未做任何归属审计以外的操作。
3. 旧 `servers/AgentTest.pem` 对应的密钥对需要持有人确认是否已轮换。
