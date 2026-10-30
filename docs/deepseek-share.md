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
- 分享链接形态：`https://chat.deepseek.com/share/<24位ID>`，ID 形如 `<18位随机字符串>`。

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
那个回答首次提出了一个**尚未关闭的上游 issue**（日志显示同步流程「已完成」看似正常，
但**全程没有上传阶段**、只跑了拉取子命令、分片上传成功而元数据文档为空
（`size:0, children:[]`）、**上传在写元数据前静默失败**）。
用户随后编辑问题并重新生成，重新生成的版本不再提这个 issue，分享链接只保留最终态。

> 也就是说：**分享链接不是「导出包的子集」，而是另一个时刻的快照。**
> 谁少谁多取决于用户在网页端做了什么编辑。

### 2. 行内引用标记 `[!reference:N]`

- 分享 API **剥离**正文里的 `[!reference:N]`，引用关系移到 `references` 数组
- 账号导出包**保留**行内标记

实测 48 个共有节点里 19 个报 TEXT-DIFF，**全部**是这个原因，
归一后正文相似度 0.959–0.991，实质一致。

**跨源比对正文前必须先剥/加回这些标记**，否则会把 19 个节点误判为内容差异。

#### ⚠️ 两条路径的标记语义**完全不同**（实测，别猜）

| | 分享链接路径 | 账号导出包路径 |
|---|---|---|
| 正文标记 | `[reference:N]` | `[!reference:N]` |
| N 是什么 | **该消息 `references[]` 数组的下标** | **无法还原** |
| `references[]` | 有，且每项的 `id` 指向具体 fragment | **没有**（RESPONSE 只有 content/type） |
| 被引用的 fragment | 多数是 TOOL_OPEN（真正读过的页面） | 同左，但 **TOOL_OPEN 载荷根本没导出** |
| 能否精确解析 | ✅ 能 | ❌ **原理上不能** |

三条必须记住的事实：

1. **N 是下标，不是 fragment id**。实测：某消息 `references[]` 有 24 项，
   正文标记最大到 23 —— 对得上。把 N 当 fragment id 解读，会让一半的标记
   指向不相干的片段。
2. **fragment id 在不同消息之间会重复**（实测同一个会话里 id=8 出现在
   四条不同消息的 fragment 上）。所以查片段必须**按消息作用域**，
   全局索引会取到「第一条同名 id」。
3. **导出包路径还原不出引用关系**。71 个行内标记里 49 个（69%）指向
   TOOL_OPEN，而导出包里 96 个 TOOL_OPEN 片段**全部没有载荷**。
   曾试过「标记 N → 该消息搜索结果列表的第 N 个」，对照分享侧真值
   **71 个里 65 个对不上**——那是把来源猜错了。
   **错误的来源比没有来源更糟**：用户会以为「这句话的依据是这篇」，
   而其实不是。正确做法是如实标「该格式无法解析」+ 给候选来源池，
   精确映射走跨源并集。

### 3. TOOL_OPEN 载荷

分享链接侧 `TOOL_OPEN` 带 `result{url,title,snippet}`；
导出包侧该载荷**根本没导出**。跨源多重集比对时这会表现为"源里有、产物里没有"——
**这是来源差异，不是打捞丢失**，不要当成 bug 修。

### 4. ⚠️ 搜索结果 URL 带引用锚点

DeepSeek 的搜索结果 URL 形如：

```
https://example.org/docs/paper.pdf#7#22#7
https://example.org/docs/paper.pdf#1#1#1
```

**同一篇文档在不同引用位置锚点不同。** 按原始 URL 建字典的后果：

| 做法 | 后果 |
|---|---|
| 按原始 URL 建键 | 同一篇文章被登记成多条记录 → **互相不补字段**；去重时静默吞掉 snippet |
| 按归一 URL 建键 | 一篇文档一条记录；不同轮次的摘要收进 `variants` |

实测（一次 296 条搜索结果的会话）：唯一原始 URL 262 个，
唯一**文档**只有 253 个；按原始 URL 去重会丢掉 **8,160 个字符（11.4%）**的 snippet。

**规则**（`deepseek_common.norm_url`，提取侧与合并侧共用）：

- 主键用归一 URL（`#` 之后是锚点，去掉）
- 原值保留在 `url_original`，锚点保留在 `url_anchor`
- 同一文档的多个 (title, snippet) 组合**全部保留**在 `variants` 里
- ⚠️ 计算 fragment 签名时必须**剥掉派生字段**（`url_normalized` / `url_anchor`），
  否则「源 vs 产物」的多重集比较会全是噪声，自证会把自己判失败

### 5. ⚠️ 占位符标题的文案会变

分享 API 的 `title` 是占位符，但**字符串不是恒定的**：
见过中文的「来自分享的对话」，也见过英文的「Shared Conversation」。

写死 `== "来自分享的对话"` 的后果：`title_is_placeholder` **静默变成 False**——
不报错，只是给出一个假的「一切正常」信号；而下游正是靠这个字段决定
「要不要人工确认标题」。

规则：`deepseek_common.is_placeholder_title` —— 归一空白后**名单匹配**。

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

> ⚠️ **命令里的 `scripts/…` 是相对于 skill 目录的**。
> 在别的目录里请用绝对路径（`[path-to-skill]/scripts/…`）——
> 本 skill 的脚本靠**自己的目录**找同目录模块，cwd 在别处时会 import 失败。


### 6.1 只读模式：只有一个链接，直接把记录读出来

**不需要浏览器、不需要账号导出、不需要 Cookie、不落盘。**
给后续「不想把上下文落盘」的 Agent 用——一条命令，输出进 stdout 即可。

```bash
# 可读全文（提问 / 搜索结果 / 回复，跳过 THINK 链）
python scripts/extract_deepseek_share.py <share_id | share_url> --print --title "<真实会话名>"

# 机器可读：rescued-session/v1 产物 JSON（含 _meta.source 溯源 + _meta.verification 自证）
python scripts/extract_deepseek_share.py <share_id | share_url> --format json

# ⭐ ChatGPT 网页版可直接上传（messages 数组 + 引用映射 + 来源清单）
python scripts/extract_deepseek_share.py <share_id | share_url> --format openai --title "<真实会话名>"

# 分享 API 原始 JSON
python scripts/extract_deepseek_share.py <share_id | share_url> --format raw
```

| `--format` | stdout 内容 | 落盘 |
|---|---|---|
| `text`（`--print` 等价） | 可读全文 | 否 |
| `json` | `rescued-session/v1` 产物 | 否 |
| `openai` | `chatgpt-import/v1` 产物（messages 数组 + 引用映射 + 来源清单） | 否 |
| `raw` | API 原始 JSON | 否 |

- **只读模式不下载附件字节**（省流量），但附件清单**仍如实报出数量与文件名**
  （`bytes_available: false`），不会写成「0 个附件」。
- `--format` / `--print` 与 `--outdir` **互斥**：要落盘用 `--outdir`，要读出来用 `--format`。
- **只读模式也自证**：`--format json` 的 `_meta.verification` 里带着多重集自证结果
  （源 fragment 签名多重集 vs 产物多重集 + `raw_messages` 逐字比对），
  不用另外跑 `--verify`。
- **`--slim`**（配合 `--format openai`）：去掉 `fragments` / `raw_*` 兜底，
  产出小文件——上传到 ChatGPT 网页版更快，但**不再是无损产物**。

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

7 条断言（snippet 字符/条目不丢、fragment/节点不丢、逐 url snippet 未被截短、
fragment 键全覆盖、**同一文档不得出现多条记录**），退出码 0 = 全过、3 = 有失败，可直接当门禁用。
详见 SKILL.md「跨源并集模式」。

## 七、附件字段约定（与导出包路径对齐）

> ⚠️ **示例一律用合成值**：早期版本这里照抄过真实会话的附件对象
> （真实 file_id 前缀、真实文件名、真实字节数、真实提问原文）——
> 那等于把一段私人对话的片段抄进了仓库。现在全部换成占位值。

```json
{
  "file_id": "file-<uuid>",
  "file_name": "Screenshot_<日期时间>.webp",
  "file_size_bytes": 0,
  "node_id": "<节点 id>",
  "timestamp": "<ISO 时间戳>",
  "question_it_answers": "【合成样例】这张图对应的那一问",
  "bytes_available_in_export": false,
  "bytes_available": true,
  "local_path": "附件/Screenshot_<日期时间>.webp",
  "sha256": "<sha256 前 16 位>",
  "downloaded_bytes": 0,
  "variants": [{"variant": "原图", "...": "..."}, {"variant": "缩略图", "...": "..."}]
}
```

`bytes_available_in_export` 恒为 `false`——**账号导出包里永远没有字节**，
这是两条路径对齐时的锚点字段。
