# 研究、回补与影子比较的输入清单

> 下文点名、但已不在工作区的历史文档，正文在 git 历史，标签 `research-archive-20261106`。


日期：2026-10-23。目的：启动前核对指定输入，避免漏设 `PEER_SUBS_DIR` 等配置后换了样本却沿用旧结果。清单由操作人事先指定，不在运行时自动接受当前文件的哈希。

## 清单格式

```json
{
  "schema_version": 1,
  "invocation": ["-m", "catalyst_data.ingestion.collectors.alpaca", "news-backfill", "--db", "/opt/catalyst/sandbox/news.sqlite3", "--dates", "/opt/catalyst/sandbox/dates.txt"],
  "files": [
    {"path": "/absolute/path/to/input", "sha256": "operator-verified-64-character-lowercase-sha256"}
  ],
  "environment": {
    "PYTHONPATH": "/opt/catalyst/collectors/packages/data",
    "CATALYST_ARCHIVE_DIR": null,
    "CATALYST_DATA_DIR": null,
    "CATALYST_RATELIMIT_DIR": null,
    "CATALYST_RESILIENCE_DB": null,
    "APCA_DATA_URL": null
  }
}
```

这是结构样例，哈希占位符不能直接运行。`null` 表示变量必须未设置；空字符串与未设置不同。Alpaca 的非秘密端点 APCA_DATA_URL 也必须核对，禁止在端点里夹带用户、密码或查询凭据。清单、结果和日志不包含密钥，也不包含密钥哈希。

- 文件必须是绝对路径；SHA-256 逐字节校验。目录的哈希是按相对路径排序的 `[relative_path, file_sha256]` 列表，用 ASCII JSON、紧凑分隔符序列化后计算 SHA-256。不得把普通文件的哈希写成目录哈希。
- 研究输入使用不可变副本。SQLite 用备份 API / `.backup` 生成自洽副本再哈希，不能复制正在写入的主文件来冒充快照；非空 WAL 会拒绝。目录内的文件增减也会改变目录哈希。
- 清单覆盖实际读取的数据库、快照、样本、公司名/同行映射、更正表，以及入口代码和输入校验代码。研究入口将实际的默认路径也列为必需输入；同一命令但缺列实际输入仍拒绝。
- `environment` 只允许安全的路径、模式等设置；凭据变量不能写值。它不声称记录了整个进程环境，API key 仍由既有秘密配置注入，不参与结果身份。
- 不允许把锁定评测目录列作 data 任务输入。既有研究代码未因此获得读两轮输出或使用测试集决策的授权。

## 启动和结果

显式设置 `CATALYST_INPUT_MANIFEST` 指定清单、`CATALYST_INPUT_RECEIPT` 指定本次新的回执路径。回执必须是尚不存在的独立输出，不能覆盖清单或任何输入。每次尝试使用不同的 job ID 和回执文件名。

启动时先校验命令、文件哈希、有效环境；失败就退出，不打开目标库或发起采集。成功后：

1. 标准输出第一行是 `{"input_manifest": {...}}`，包含实际校验的路径、哈希、环境和清单 SHA-256。
2. 同样的回执保存为独立 JSON；保留它与结果一起交付。原有结果文件格式保持不变，回执通过 job ID 关联。
3. 此后才生成结果或运行回补。启动校验不代替文件隔离：运行期间不得有其他任务修改输入副本。

跨任务等待继续用 `catalyst-job NAME ...` 与 `catalyst-job --wait NAME`；不靠临时 systemd 单元或 pgrep 判断完成。联网批量任务仍只在美国服务器上、允许窗口内运行，设置 RuntimeMaxSec。

## 分支与覆盖边界

- `data/input-preflight`：九个 `scripts/research/v3/` 入口的启动校验；历史抓取失败统计改读事先过滤的、已哈希的状态/错误日志副本，不能直接 cat/tail 评测日志。
- `data/collectors`：手动 Alpaca 新闻回补、研究分钟线、显式日期/结算日的 FINRA 和监管回补。正式的周期采集入口保持原有调用形式。
- `data/time-fix`：手动 `daily run/retry/deferred` 的输入校验；`daily scheduled` 不改变。
- `shadow_compare.py`：新验收入口已在 data/collectors 接入同一校验器，本地验证通过但尚未部署。其适配范围、运行记录缺口和阻止切换的条件见 shadow_acceptance.md；旧服务器入口不能算已受保护。
- 本稿描述的代码均须在单项汇报中给出提交和部署状态；本地有测试不等于服务器已启用。冻结运行器、其他旧研究目录和任意 Python 函数调用不属于本轮入口覆盖，不得用它们绕过本规则启动新任务。

## 固定输入与真实联网分开

固定输入回放锁定全部响应/数据库/配置，用于归因行为差异。真实联网沙盒同时记录采集开始/结束、服务端响应身份和上述输入回执，只证明提供方当前可用；不能因为输入清单相同，就宣称实时网络结果必然相同。新旧日更版本的这两类报告分别交负责人确认。
