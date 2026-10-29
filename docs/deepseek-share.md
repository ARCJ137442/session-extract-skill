# DeepSeek 网页版分享链接路径

> 与 `scripts/extract_deepseek.py`（账号导出包路径）**互补**的两条提取路径。
> 本文档只讲**分享链接路径**独有或两条路径必须对齐的部分。

## 一、为什么需要第二条路径

| | 账号导出包 | 分享链接 |
|---|---|---|
| 前置条件 | 用户要导出账号数据 | 只要一个链接 |
| 会话范围 | 该账号**全部**会话 | 单个会话 |
| 结构 | 聊天树 `mapping` | 线性消息数组 |
| **附件字节** | ❌ 只有元数据 | ✅ **可直下** |
| **TOOL_OPEN 载荷** | ❌ 未导出 | ✅ 完整 |
| 真实会话名 | ✅ 有 | ❌ 占位符「来自分享的对话」 |
| 消息时序 | 某一时刻的快照 | 某一时刻的快照（**与前者不是同一时刻**） |

**两条路径的并集才是完整历史**，任何单条路径都不完整。

## 二、端点

```
会话正文  GET https://chat.deepseek.com/api/v0/share/content?share_id=<ID>
附件原图  GET https://files.deepseeksvc.com/api<signed_path>&ty=p
附件缩略  GET https://files.deepseeksvc.com/api<signed_path>&ty=t
```

- 全部**无需登录**，只需常规浏览器 `User-Agent` + `Referer: https://chat.deepseek.com/`。
- ⚠️ **`/api/v0/share?share_id=` 是失效端点**：返回 200，但内容是 SPA 首页 HTML。
  脚本里显式拦了这个情况（见 `fetch_share()` 的 JSON 前缀检查），别改回老接口。
- 分享链接形态：`https://chat.deepseek.com/share/<24位ID>`，ID 形如 `<18位ID>`。

## 三、⚠️ 通用套路：附件 URL 不在 DOM 里时，去前端 bundle 反查

这是本路径最值钱的一条**可迁移**知识，不是 DeepSeek 专用。

**现象**：分享页**完全不渲染附件**——DOM 里查不到任何 `<img>` 或附件节点，
`performance.getEntriesByType('resource')` 里也没有附件请求。
但 JSON 的 `fragments[].files[].signed_path` 里明明带着签名路径。

**为什么**：DeepSeek 把附件放在独立文件服务上，域名与路径前缀**不在页面里**，
只在前端 bundle 的 URL 构造逻辑中。

**怎么找到的**（可复用的步骤）：

1. 抓前端 bundle：`<页面静态资源域名>/<路径>/main.<hash>.js`
   （DeepSeek 是 `https://fe-static.deepseek.com/chat/static/main.d1d163b7e4.js`）
2. 在 bundle 里搜字段名：`grep -o -E ".{150}signedPath.{250}" main.js`
3. 顺着找到 URL 构造函数 `dp()`，读出它的拼接逻辑：

   ```js
   dp = e => {
     let {signedPath: r, type: s} = e;
     if (!r) return null;
     return "https://" + getFilesBaseDomain() + "/api" + signedPath + "?ty=" + s;
   }
   ```
4. 再找 `getFilesBaseDomain()` 的默认值：
   `dr = "files.deepseeksvc.com"`（dev 环境 `files-dev.deepseeksvc.com`，
   test 环境 `files-test.deepseeksvc.com`）
5. 拼出真实端点并验证 `Content-Type` 是 `image/*` 而不是 `text/html`

**通用化**：任何「API 返回了资源路径但页面不渲染」的站点，
都在前端 bundle 里搜 `signedPath` / `path` / `url` 这类字段，反查它的 URL 构造函数。
**比在 DOM 里瞎找快一个数量级。**

## 四、两条路径的已知差异（跨源比对前必须归一）

### 1. 消息集合不同——并集才是完整历史

实测同一会话「目标会话」：

| | 分享链接 | 账号导出包 |
|---|---|---|
| 消息数 | 48 | **50** |
| fragment 数 | 241 | **248** |
| 独有 | 3 个 `TIP` | **第 47/48 号整轮** |

导出包多出的第 47/48 号轮次，是用户**编辑问题之前**的原始提问 + 回答；
那个回答首次提出 **GitHub Issue 某 issue**（日志显示 `同步流程日志` 看似正常，
但**全程无 push 阶段**、只跑 `pullOnly`、chunks 上传成功而 `f:` 元数据文档为空
（`size:0, children:[]`）、**上传在写元数据前静默失败**）。
用户随后编辑问题并重新生成，重新生成的版本不再提 某 issue，分享链接只保留最终态。

> 也就是说：**分享链接不是「导出包的子集」，而是另一个时刻的快照。**
> 谁少谁多取决于用户在网页端做了什么编辑。

### 2. 行内引用标记 `[!reference:N]`

- 分享 API **剥离**正文里的 `[!reference:N]`，引用关系移到 `references` 数组
- 账号导出包**保留**行内标记

实测 48 个共有节点里 19 个报 TEXT-DIFF，**全部**是这个原因，
归一后正文相似度 0.959–0.991，实质一致。

**跨源比对正文前必须先剥/加回这些标记**，否则会把 19 个节点误判为内容差异。

### 3. TOOL_OPEN 载荷

分享链接侧 `TOOL_OPEN` 带 `result{url,title,snippet}`；
导出包侧该载荷**根本没导出**。跨源多重集比对时这会表现为"源里有、产物里没有"——
**这是来源差异，不是打捞丢失**，不要当成 bug 修。

## 五、跨源并集怎么做

两条路径都输出 `rescued-session/v1`，可直接对齐：

| 字段 | 导出包 | 分享链接 |
|---|---|---|
| `conversations[0].id` | 会话 UUID | share_id |
| `conversations[0].transcript[].node_id` | 树节点 id | `message_id`（字符串） |
| `conversations[0].transcript[].sources` | `["export"]` | `["share"]` |
| `conversations[0].attachments[].bytes_available` | `false` | `true` |
| `conversations[0].attachments[].local_path` | `null` | `附件/<file>` |
| `conversations[0].raw_*` | `raw_mapping`（树） | `raw_messages`（数组） |

⚠️ **`raw_*` 键名不同**：分享链接没有聊天树，就是一个线性数组，所以叫 `raw_messages`。
并集代码要同时认这两个键。

`_meta.sources[]` 列出本路径覆盖的节点（`node_ids`），跨源并集时取并集即可，
并给每个 transcript 条目标上它来自哪条路径（`sources`）。

## 六、用法

### 6.1 只读模式：只有一个链接，直接把记录读出来

**不需要浏览器、不需要账号导出、不需要 Cookie、不落盘。**
给后续「不想把上下文落盘」的 Agent 用——一条命令，输出进 stdout 即可。

```bash
# 可读全文（提问 / 搜索结果 / 回复，跳过 THINK 链）
python scripts/extract_deepseek_share.py <share_id | share_url> --print --title "<真实会话名>"

# 机器可读：rescued-session/v1 产物 JSON（含 _meta.source 溯源）
python scripts/extract_deepseek_share.py <share_id | share_url> --format json

# 分享 API 原始 JSON
python scripts/extract_deepseek_share.py <share_id | share_url> --format raw
```

| `--format` | stdout 内容 | 落盘 |
|---|---|---|
| `text`（`--print` 等价） | 可读全文 | 否 |
| `json` | `rescued-session/v1` 产物 | 否 |
| `raw` | API 原始 JSON | 否 |

- **只读模式不下载附件字节**（省流量），但附件清单**仍如实报出数量与文件名**
  （`bytes_available: false`），不会写成「0 个附件」。
- `--format` / `--print` 与 `--outdir` **互斥**：要落盘用 `--outdir`，要读出来用 `--format`。

### 6.2 落盘模式（含附件字节）

```bash
# 独立用（模块自带 CLI）
python scripts/extract_deepseek_share.py <share_id | share_url> \
    --outdir <dir> [--title "<真实会话名>"] [--markdown] [--verify]

# 统一调度（由 session_extract.py 接线）
python scripts/session_extract.py --platform deepseek-share <share_id | share_url> \
    [--outdir <dir>] [--markdown]
```

- `--title`：**分享 API 的 `title` 是占位符「来自分享的对话」**，
  真实会话名要打开网页看侧边栏确认后再传进来；
  传了之后 `title_is_placeholder` 会是 `false`。
- `--verify`：多重集自证（没丢没重 + raw 一致 + 附件字节到位）。

### 6.3 跨源并集后的门禁自检

```bash
python scripts/verify_merged_deepseek.py --export <导出包产物.json> \
    --share <分享链接原始.json> --merged <并集产物.json> [--outdir <目录>]
```

6 条断言（snippet 字符/条目不丢、fragment/节点不丢、逐 url snippet 未被截短、
fragment 键全覆盖），退出码 0 = 全过、3 = 有失败，可直接当门禁用。
详见 SKILL.md「跨源并集模式」。

## 七、附件字段约定（与导出包路径对齐）

```json
{
  "file_id": "file-<附件 id>-...",
  "file_name": "Screenshot_2026-10-29-02-12-02-...webp",
  "file_size_bytes": <字节数>,
  "node_id": "21",
  "timestamp": "2026-10-29T02:12:39.017000+08:00",
  "question_it_answers": "【合成样例】这张图对应的那一问",
  "bytes_available_in_export": false,
  "bytes_available": true,
  "local_path": "附件/Screenshot_2026-10-29-02-12-02-...webp",
  "sha256": "<sha256 前缀>",
  "downloaded_bytes": <字节数>,
  "variants": [{"variant": "原图", "...": "..."}, {"variant": "缩略图", "...": "..."}]
}
```

`bytes_available_in_export` 恒为 `false`——**账号导出包里永远没有字节**，
这是两条路径对齐时的锚点字段。
