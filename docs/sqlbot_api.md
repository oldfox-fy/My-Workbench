# SQLBot 模型管理与问数接口清单（M0 产物）

> 来源：dataease/SQLBot 源码（`backend/apps/system/api/aimodel.py`、`backend/apps/mcp/mcp.py`、
> `backend/apps/system/middleware/auth.py`、`backend/apps/system/schemas/*`、`frontend/src/entity/supplier.ts`）。
> 梳理日期：2026-11-12。

## 0. 版本与前提

- 全局 API 前缀：`/api/v1`（`CONTEXT_PATH` 默认为空）。
- 默认管理员账号：`admin` / `SQLBot@123456`（`id=1`，`account='admin'` → `isAdmin=True`）。
- 判定管理员的逻辑：`get_user_info` / `mcp.get_user` 均为 `isAdmin = (id == 1 and account == 'admin')`。
- **端口区分（M3 实测）**：REST 接口（模型管理 `/api/v1/system/*`、`/api/v1/mcp/access_token`）
  在 **8000** 端口（Web 控制台同一个 FastAPI 应用）；MCP SSE 在 **8001**（`/mcp`）。两者不是同一端口。
  My Workbench 里 `rest_url` 指向 8000，`mcp_config.json` 的 `url` 指向 8001。
- **默认模型不可删（M3 实测）**：`DELETE /system/aimodel/{id}` 对 `default_model=true` 的模型返回 500
  `Cannot delete the default model`。删除前需先 `PUT /system/aimodel`（完整 payload，`default_model:false`）
  取消默认。`PUT /system/aimodel/default/{id}` 会把默认资格转移到新模型（取消旧默认）。

## 1. 认证（关键）

### 1.1 拿 token
```
POST /api/v1/mcp/access_token
body: { "username": "admin", "password": "SQLBot@123456" }   # 明文
=> { "access_token": "<JWT>" }
```

### 1.2 调 REST 接口的鉴权头
```
X-SQLBOT-TOKEN: Bearer <JWT>
```
- **不是**标准 `Authorization: Bearer`，是自定义头 `X-SQLBOT-TOKEN`，值仍是 `Bearer <jwt>` 格式。
- **关键结论**：MCP `access_token` 返回的 JWT 与 REST 接口使用**同一个 SECRET_KEY**，payload 含
  `{id, account, oid}`（`BaseUserDTO.to_dict()`），可直接复用为 REST 的 Bearer token。
  即：登录一次，token 同时用于模型管理 REST 和问数 MCP。
- 备用鉴权头（本次集成用不到）：`X-SQLBOT-ASK-TOKEN: sk <apikey-jwt>`、`X-SQLBOT-ASSISTANT-TOKEN: assistant/embedded <jwt>`。

## 2. 模型管理接口（需 admin，前缀 `/api/v1/system/aimodel`）

### 2.1 请求体字段（AiModelCreator / AiModelEditor）

| 字段 | 类型 | 说明 | My Workbench 映射 |
|---|---|---|---|
| `name` | str | 显示名 | `models.name` |
| `model_type` | int | 模型类型，**0 = 对话** | 固定 `0` |
| `base_model` | str | 模型名 | `models.modelName` |
| `supplier` | int | 服务商，**15 = 通用OpenAI** | 固定 `15` |
| `protocol` | int | **1 = openai 兼容**，2 = vllm | 固定 `1` |
| `api_domain` | str | base URL | `models.baseUrl` |
| `api_key` | str | 密钥（可空） | `models.apiKey` |
| `default_model` | bool | 是否默认（首个模型会自动置默认） | 首个 `true` |
| `config_list` | list | 额外参数 `[{key,val,name}]`，**必传**，可为 `[]` | 可选传 temperature |

> 备注：`supplier=15`（通用OpenAI）是给任意 OpenAI 兼容端点用的，默认 `api_domain` 为
> `http://127.0.0.1:8000/v1`，我们直接覆盖为 My Workbench 的 `baseUrl`（本地 Ollama 的 `/v1` 同样适用）。

### 2.2 接口列表

| 方法 | 路径 | 用途 | 请求体/说明 |
|---|---|---|---|
| POST | `/system/aimodel` | 新增模型 | `AiModelCreator`，返回 `AiModelDetail`（含 `id`） |
| PUT | `/system/aimodel` | 更新模型 | `AiModelEditor`（含 `id`） |
| DELETE | `/system/aimodel/{id}` | 删除模型 | 默认模型不可删；需处理工作空间影响 |
| GET | `/system/aimodel` | 模型列表 | 返回 grid（id/name/base_model/supplier/default_model 等） |
| GET | `/system/aimodel/{id}` | 模型详情 | `api_key`/`api_domain` 会被解密返回 |
| PUT | `/system/aimodel/default/{id}` | 设为默认模型 | — |
| POST | `/system/aimodel/status` | 测试连通 | `AiModelCreator`，流式 NDJSON，内部用 `1+1=?` 探测 |

### 2.3 工作空间绑定（**必须做**）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/system/aimodel/{id}/ws_mapping` | 追加绑定，body `["<ws_id>", ...]`（字符串数组） |
| PUT | `/system/aimodel/{id}/ws_mapping` | 覆盖绑定，body 同上 |
| GET | `/system/aimodel/{id}/ws_mapping` | 查看已绑定的 workspace_id 列表 |
| DELETE | `/system/aimodel/{id}/ws_mapping` | 删除指定绑定 |
| GET | `/system/aimodel/list/by_ws` | 按当前用户工作空间列出模型（`ws_admin` 权限） |

> **为什么必须绑定**：`resolve_model_id` → `require_authorized_model` 会校验 `custom_model` 是否存在于
> `ai_model_workspace_mapping`（model_id + workspace_id），否则抛 `model_unavailable`。
> 所以同步流程 = **建模型 → 绑定到工作空间**，两步缺一不可。

## 3. 问数接口（前缀 `/api/v1/mcp`）

| 方法 | 路径 | 请求体 | 返回 |
|---|---|---|---|
| POST | `/mcp/access_token` | `{username, password}` | `{access_token}` |
| POST | `/mcp/mcp_ws_list` | `{token}` | 工作空间列表 |
| POST | `/mcp/mcp_ds_list` | `{token, oid?}` | 数据源列表 |
| POST | `/mcp/mcp_model_list` | `{oid}` | 该工作空间可用模型（`id`(str)/`name`/`default_model`/`supplier`） |
| POST | `/mcp/mcp_start` | `{token, oid?}` | `{access_token, chat_id}` |
| POST | `/mcp/mcp_question` | `{token, chat_id, question, datasource_id?, custom_model?, stream?, return_img?}` | 问数结果 |

> `custom_model` 类型为 `str | int`，含义是 **SQLBot 模型的 id**（对应 `ai_model.id`）。
> 问数时传它即可指定"当前要用的模型"——这正是"模型实时同步"的落点。

## 4. M0 验证结论

1. ✅ **模型同步可行**：SQLBot 有完整模型 CRUD + 测试连通 REST 接口，用 admin JWT 即可程序化调用。
2. ⚠️ **必须做 ws_mapping**：新模型要绑定到工作空间，否则 `custom_model` 传参报 `model_unavailable`。
3. ✅ **鉴权可统一**：MCP `access_token` 的 JWT 直接复用为 REST 的 `X-SQLBOT-TOKEN: Bearer`，一次登录两用。
4. ✅ **custom_model = SQLBot 模型 id**：My Workbench 侧需维护「My Workbench 模型 id ↔ SQLBot 模型 id」映射表。
5. ⚠️ **加密细节**：创建/更新时 POST 明文即可，服务端在启动时对 `api_domain`/`api_key` 做异步加密；`GET /{id}` 会返回解密后的明文。

## 5. 同步流程伪代码（M3 参考）

```
def sync_models_to_sqlbot():
    token = mcp_access_token(admin_user, admin_pwd)        # 拿 JWT
    for wb_model in list_my_workbench_models():
        sqlbot_model_id = map_table.get(wb_model.id)
        payload = {
            "name": wb_model.name,
            "model_type": 0,
            "base_model": wb_model.modelName,
            "supplier": 15,
            "protocol": 1,
            "api_domain": wb_model.baseUrl,
            "api_key": wb_model.apiKey,
            "default_model": is_first_model,
            "config_list": [],                               # 或 [{"key":"temperature","val":0.6,"name":"温度"}]
        }
        if sqlbot_model_id is None:
            detail = POST /system/aimodel  (payload)         # 新增 → 拿 id
            POST /system/aimodel/{id}/ws_mapping  body=["<ws_id>"]  # 绑定工作空间
            map_table[wb_model.id] = detail.id
        else:
            PUT /system/aimodel  ({**payload, "id": sqlbot_model_id})  # 更新
    # 删除：My Workbench 已删但 SQLBot 还在的模型
    for orphan in find_orphans():
        DELETE /system/aimodel/{orphan.sqlbot_model_id}
```

> 问数时：`system_sqlbot_ask` 查当前活跃模型 → `map_table` 拿 `sqlbot_model_id` → 作为 `custom_model` 传给 `mcp_question`。

## 6. 数据源管理接口（M4）

数据源由 My Workbench 设置面板「智能问数」代理到 SQLBot，复用同一个 admin JWT（`X-SQLBOT-TOKEN` 头）。

| 方法 | SQLBot 路径 | 用途 | My Workbench 代理 |
|---|---|---|---|
| POST | `/mcp/mcp_ds_list` | 列数据源（body `{token, oid?}`，剥离 configuration 等敏感字段） | `GET /api/sqlbot/datasources` |
| POST | `/datasource/add` | 新增（body `CreateDatasource`） | `POST /api/sqlbot/datasources` |
| POST | `/datasource/check` | 测试连接（body `CoreDatasource`，不落库） | `POST /api/sqlbot/datasources/check` |
| POST | `/datasource/delete/{id}/{name}` | 删除 | `DELETE /api/sqlbot/datasources/{id}` |

### 6.1 `configuration` 字段加密（关键）

数据源的 `configuration` 是「AES-ECB-PKCS7 加密 + base64」的 JSON 字符串，密钥硬编码
`b'SQLBot1234567890'`（与 SQLBot 前端 `frontend/src/views/ds/js/aes.ts` 的 CryptoJS 完全一致）。
明文 JSON 字段：

```json
{
  "host": "127.0.0.1", "port": 3306, "username": "root", "password": "***",
  "database": "db", "dbSchema": "", "extraJdbc": "",
  "ssl": false, "timeout": 30, "poolSize": 5, "engine": "hive"
}
```

My Workbench 侧用 `cryptography` 库复刻加密：`backend/services/sqlbot_datasource.py::encrypt_configuration`。

### 6.2 请求体字段（CreateDatasource）

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 数据源显示名（必填） |
| `description` | str | 描述 |
| `type` | str | 数据库类型，见 6.3 |
| `configuration` | str | 加密后的 JSON（见 6.1） |
| `oid` | int | 工作空间 id，默认 1 |
| `tables` | list | 选择的表，新增时传 `[]`（`add` 会自动拉取全表并写 `num`） |
| `recommended_config` | int | 推荐问题开关，默认 1 |

### 6.3 支持的数据库类型

`mysql` / `pg` / `sqlServer` / `oracle` / `ck` / `doris` / `starrocks` / `hive` / `dm` / `kingbase`。
完整列表见 `backend/services/sqlbot_datasource.py::DATASOURCE_TYPES`（含默认端口）。

### 6.4 注意事项

- `add` 会实际连接数据库拉取表结构（`create_ds → updateNum → get_tables`），连接失败时返回 500；
  但数据源记录可能已先 commit，之后刷新列表会看到一条 `num` 为空的数据源（SQLBot 自身行为）。
- `check` 失败时 SQLBot 直接返回 500 + 纯文本 `detail`（**不是** `{code,data,msg}` 信封），
  代理层用 `_plain_detail` 还原可读文本。
- 图表图片 URL（`mcp_question` 返回的 `image_url`）形如 `http://localhost:8001/images/c_{chat_id}_r_{record_id}.png`，
  由 MCP 服务（8001）静态托管，前端可直接用 Markdown `![图表](url)` 渲染。
