# AutoDL 目标配置与模型路由报告（2026-10-30）

## 1. 结论

`Target` 现在完整消费 `kind` / `product` / `ssh_host` / `ssh_port`，
`services.json` 明确记录 `product=autodl` 与 `ssh_port=12792`。
AutoDL 目标的专用模型路由已接上，并在未配置时给出明确信号。

状态：`VERIFIED`（字段解析与路由均有实际输出）。

## 2. 改动前后

| 字段 | 改动前 | 改动后 |
|---|---|---|
| `kind` | 有（上一轮新增） | 有 |
| `product` | **无** | 有，小写归一 |
| `ssh_host` | 有 | 有 |
| `ssh_port` | **无**（只存在于全局 `SSH_PORT`） | 有，非法值回落 0 |

改动前的实际后果（均实测）：

- **端口无法在目录里表达**：`services.json` 写不了"这台机器走 12792"，
  连接端口永远取进程环境里的 `SSH_PORT`；把 `.env` 改成别的端口不会有任何
  校验或报错。
- **`product` 完全没有**：`Target(..., product="autodl")` 会抛 `TypeError`，
  `_coerce_entry` 静默丢弃 `product` 键。于是 `kind=ssh` 无法区分
  "AutoDL 的 SSH 主机"与"任何其它 SSH 主机"，专用模型路由只能靠 kind 判断。

## 3. 当前 services.json（不含任何凭据）

```json
{
  "service_name": "autodl-ssh",
  "instance_id": "autodl-container-73d6479980-a8bf3ae3",
  "region": "seetacloud-sha1",
  "status": "active",
  "type": "ssh",
  "product": "autodl",
  "ssh_host": "connect.sha1.seetacloud.com",
  "ssh_port": 12792
}
```

**不包含** key、密码、token 或本机绝对密钥路径——密钥路径只在运行时环境
（`SSH_KEY_FILE`）里，不进目录。

实测解析结果：

```
kind=ssh  product=autodl  ssh_port=12792  ssh_only=True  ssh_in_scope=True
```

## 4. 模型路由

规则（任务书 §4.1）：

```
target.kind=ssh + product=autodl  ->  qwen15-autodl-ssh-v1（LOCAL_LLM_MODEL_SSH）
其它目标                          ->  通用 LOCAL_LLM_MODEL
cloud_multi                       ->  在线多智能体
```

两个模型**共用同一个 `LOCAL_LLM_BASE_URL`**，靠 vLLM 的 `--served-model-name`
区分。不引入第二个端点配置——那会变成两套需要同步的连接参数。

### 4.1 未配置时的信号（任务书 §4.2）

原实现：`LOCAL_LLM_MODEL_SSH` 为空 → 静默退回通用模型，**没有任何提示**。
用户会以为 AutoDL 专用 adapter 已生效。

现在：`kind=ssh` 但未配置专用模型时写一条 trace：

```
event: local_model_ssh_unconfigured
note: target 是 SSH 主机但 LOCAL_LLM_MODEL_SSH 未配置，
      本次使用通用模型；AutoDL 专用 adapter 未生效
```

## 5. 运行配置状态（只记 set/unset，不含值）

| 变量 | 状态 |
|---|---|
| `LOCAL_LLM_BASE_URL` | set |
| `LOCAL_LLM_MODEL` | set（通用模型） |
| `LOCAL_LLM_MODEL_SSH` | set（`qwen-autodl`） |
| `SSH_HOST` | set |
| `SSH_PORT` | set（12792） |
| `SSH_USER` | set |
| `SSH_KEY_FILE` | set（密钥在仓库外，被 `.tmp*/` 覆盖） |

## 6. 未完成 / 已知限制

1. **`product` 只用于路由判断，尚未贯通到工具侧**：`tools_for_target()` 仍按
   `kind` 收窄，没有按 `product` 再细分。当前只有一个 SSH 产品，无实际影响。
2. **`ssh_port` 尚未真正用于连接**：`servers/ssh_server.py::_get_ssh_client()`
   仍读全局 `SSH_PORT`。目录里的 `ssh_port` 目前只作为目标元数据存在，
   没有做"目录值 vs 全局值"的一致性校验。这是任务书 §2.1 第 7 条未闭环的部分。
3. 单 API Key 身份模型不变；`principal` 隔离只用于资源归属验证。
