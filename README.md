# 简笔画涂鸦 · astrbot_plugin_svg_doodle

> 她写 SVG，程序渲染成蜡笔简笔画 —— **不经过任何生图模型**。

让她亲手画一张涂鸦给自己、给你、给群里。模型只负责写矢量代码，渲染、加纸张质感、签名、存档全由程序完成。

---

## 为什么不用生图模型

| | 生图模型 | 本插件 |
|---|---|---|
| 中文渲染 | 扩散模型不会写汉字 | SVG `<text>` 直接写 |
| 一致性 | 每次角色长得都不一样 | 外形描述固定，每次都像同一个 |
| 可编辑 | 生成即定型 | 存了原始 SVG，随时改 |
| 成本 | 按张计费 | 免费（本地模型即可） |
| 速度 | 15~40s | 8~17s |

关键突破是 **cairosvg 不支持 `<filter>`**（feTurbulence / feDisplacementMap 实测完全无效），所以蜡笔质感必须在渲染之后用 PIL 做像素级后处理。本插件的 `crayon.py` 就是这一步。

---

## 功能

- 🖍 **LLM 写 SVG**：模型输出纯 SVG 代码，程序用 cairosvg 渲染成 PNG
- 🎨 **蜡笔质感**：线条抖动（`edge_jitter`）+ 纸张颗粒（`grain_strength`）+ 边缘粗糙（`stroke_rough`），可按档调节
- ✍️ **签名与日期**：右下角自动署 `— 签名 日期`
- 📚 **画册**：所有画作按时间倒序保存，可在 WebUI 里浏览、查看原图
- 🔍 **翻旧画**：`recall_doodle` 工具，按关键词找以前画过的
- 🧑‍🎨 **人格感知**：对话里画「她自己」时会读取当前会话的真实人格 prompt（WebUI 测试不注入人格，只用配置的外形）
- 🎯 **画别人**：说「画用户」/「画老大」时自动切换成「普通人画法」，不会把自己画上去
- ⚙️ **完全可自定义**：24 项配置，角色外形、画风、约束、画布、纹理、签名、目录全可改
- 🤖 **任意模型**：可以选 AstrBot 里配好的任意对话模型，也可以填「自定义」直接用 OpenAI 兼容端点

---

## 安装

### 方式一：AstrBot 插件市场

在 WebUI 的插件市场搜索「简笔画涂鸦」安装。

### 方式二：手动

```bash
cd /path/to/astrbot/data/plugins
git clone https://github.com/yangtuoor/FIIIIX_PLUGIN.git
# 把 astrbot_plugin_svg_doodle 目录拷出来放到 data/plugins/ 下
```

### 依赖

```bash
pip install cairosvg numpy pillow
```

Ubuntu 上 cairosvg 还需要系统库：

```bash
apt install -y libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev
```

**中文字体**（签名用，默认值指向 Noto）：

```bash
apt install -y fonts-noto-cjk
# 默认 font_path: /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
```

装好后重启 AstrBot，插件页会出现在侧边栏「简笔画涂鸦」。

---

## 使用

### 对话里让她画

她会自己决定要不要画。触发后调用 `draw_doodle` 工具：

```
你：画个坐在海边吃白米饭的
她：（调用 draw_doodle，subject="坐在海边礁石上捧着碗吃白米饭"）
    画好啦，坐在礁石上扒白米饭，尾巴盘在脚边，夕阳的光暖黄黄地落在身上
```

### 命令直接画

```
涂鸦 坐在海边看海
画图 一只橘猫晒太阳
画画 雨天打伞
```

### 翻旧画

```
之前画过哪些？
```

她会调用 `recall_doodle` 按关键词查画册。

---

## WebUI

插件页面分四个 tab：**测试画图 / 配置 / 画册 / 说明**。

### 测试画图

左侧填要画什么，右侧实时预览。点「开始画图」直接跑一次，不占用对话额度。

**注意：WebUI 测试不注入人格**，只用配置里 `character_desc` 的外形。所以这里画出来的永远是配置版角色，而对话里画出来会跟当前人格走。

### 配置

24 项全部可改，分组如下：

| 分组 | 配置项 |
|---|---|
| 模型 | `provider_id`、`custom_model_name`、`custom_api_base`、`custom_api_key`、`custom_extra_body` |
| 角色 | `character_name`、`character_desc`、`style_desc`、`body_constraint`、`scene_prefix` |
| 输出 | `output_suffix`、`canvas_size`、`max_retry` |
| 权限 | `enabled_groups`、`allow_private` |
| 画质 | `texture_strength`、`edge_jitter`、`grain_strength`、`stroke_rough` |
| 水印 | `signature`、`show_date`、`font_path` |
| 存储 | `gallery_dir`、`gallery_limit` |

### 画册

按时间倒序展示所有画作（缩略图 360px，点击看原图）。缩略图服务端生成并缓存到 `gallery_dir/thumbs/`，首次生成后走缓存。

---

## 配置详解

### 模型选择

`provider_id` 有三种填法：

| 值 | 含义 |
|---|---|
| （空） | 跟随当前会话的默认模型 |
| `deepseek/deepseek-chat` 等 | AstrBot 里已配好的任意对话模型 |
| `__custom__` | 走下面四项自定义参数 |

选 `__custom__` 时展开：

```
custom_model_name  = deepseek-chat
custom_api_base    = http://127.0.0.1:18900/v1
custom_api_key     = sk-anything
custom_extra_body  = {"temperature": 0.9}
```

`custom_api_base` 支持填到 `/v1` 或完整的 `/v1/chat/completions`，两种都认。

### 外形描述

`character_desc` 是整个插件最关键的一项。写得越像「形状清单」，模型画得越准：

```
圆脑袋，两个小圆点当眼睛，一个向上弯的弧线当笑脸，两块粉色腮红。
深蓝色头发盖住头顶大半，头顶翘一根小呆毛。
头顶白色蕾丝小帽子（白色波浪线横跨头顶）。
头两侧各一个鲸鱼鳍耳朵（小三角形，尖朝外，深蓝，边长 25px）。
蓝色梯形裙子，白色围裙（小白方块）。身后一条鲸鱼尾巴（两片弧形，深蓝）。
```

**反例**（模型画不出来）：

```
一只很可爱的鲸鱼娘，有漂亮的蓝色长发和耳朵
```

——「鲸鱼鳍形状」这种抽象描述会被画成甜甜圈。

### 身体结构约束（重要）

`body_constraint` 的默认值解决了一个困扰很久的问题：**头和身体老分开**。

❌ 「画个脖子」→ 出现两条竖线，头和身体分离
❌ 「不要脖子」→ 头悬空，路易十六
✅ 「**头直接坐在身体上，重叠 20px，先画身体后画头，头盖住身体顶部**」

关键是描述的「图层绘制顺序」，不是「有没有脖子」这个概念。

### 画质参数

| 参数 | 效果 | 推荐值 |
|---|---|---|
| `texture_strength` | 蜡笔斜向笔触强度 | `0`（只要噪点）/ `0.18`（轻）/ `0.30`（明显） |
| `edge_jitter` | 线条抖动幅度（像素） | `1.8`（`2.5` 更像手画，`5.0` 像小朋友） |
| `grain_strength` | 纸张颗粒强度 | `14` |
| `stroke_rough` | 线条边缘粗糙度 | `1.0` |

`texture_strength=0` 时是「彩色铅笔在纸上画」的感觉，干净但有纸张纹理。

---

## 对话里画谁

`draw_doodle` 的 `subject` 参数会判断画谁：

| 关键词 | 判定 | 行为 |
|---|---|---|
| 我、自己、你、本鱼、自画像 | `self` | 注入 `character_desc` 的外形 |
| 用户、老大、主人、群主、他、她 | `other` | 注入「普通人画法 + 禁止清单」，不会画成角色自己 |
| 其他 | `auto` | 注入外形，但提醒「第三方必须画成普通人」 |

**对话场景额外注入人格**：`_get_persona_prompt(event)` 会调 `persona_manager.resolve_selected_persona()`，优先级是「会话强制人格 > 会话人格 > 全局默认人格」，把当前人格的 `prompt` 以最高优先级塞进系统提示，外形描述降级为「参考底稿，冲突时以人格为准」。

所以如果人格里写了「我是一条鲸鱼，深蓝长发、有鲸鱼鳍耳朵、身后有尾巴」，对话里画出来的就是那个人设版本，而不是配置版。

---

## 数据目录

```
data/plugin_data/astrbot_plugin_svg_doodle/
├── gallery.json                 画册索引
├── images/                      原始 PNG
│   ├── doodle_1793377655481.png
│   └── doodle_1793377655481.svg   （原始 SVG，可再编辑）
└── thumbs/                      缩略图缓存（360px JPEG）
    └── doodle_1793377655481_w360.jpg
```

`gallery_limit` 控制最多保留多少张，超出的从旧往新删。

---

## WebUI 插件页开发笔记

AstrBot 的插件前端 bridge 有两个坑，记录一下：

**1. API 前缀不是 `/api/plug/...`**

bridge 的 `apiGet(endpoint)` 实际请求：

```
/api/v1/plugins/extensions/<插件名>/<endpoint>
```

**2. bridge 会剥掉响应外壳**

服务端返回 `{"status":"ok","data":{...}}`，到前端会变成 `{...}`（没有 `status` 字段）。所以判断成功不能看 `status === 'ok'`，要看有没有 `status` 字段：

```js
function unwrap(r) {
  if (r && typeof r === 'object' && 'status' in r) {
    return { ok: r.status === 'ok', msg: r.message || '', body: r.data };
  }
  return { ok: true, msg: '', body: r };
}
```

**3. bridge 没有 `assetUrl` 方法**

只能用 `ready/getContext/getLocale/getI18n/t/onContext/apiGet/apiPost/upload/download/subscribeSSE`。图片要走 `apiGet` 拿 base64，或者服务端直接返回 `data_uri`。

---

## ⭐ 特别鸣谢

这个插件的整套思路，**直接来自 [`kana-kwi/astrbot_plugin_xiaoyu_doodle`](https://github.com/kana-kwi/astrbot_plugin_xiaoyu_doodle)（鲸小鱼涂鸦 v2.1.1）**。

原作者 kana-kwi 提出的核心理念是：

> **她自己算每一笔的坐标，程序照着画出来——不经过任何生图模型。**

这句话是整条技术路线的起点。我一开始也照这个思路做（ops + JSON 图元），但发现图元拼贴画不出连续的线，于是：

1. 先在原插件上加 `curve`（三次贝塞尔）和 `smooth`（Catmull-Rom）两种原语，缓解了线条僵硬的问题
2. 后来试了 qwen3.6-flash / qwen3.7-max，前者只画得出一个圆，后者 12~19 秒还经常超时
3. 最后转向 **让模型直接写 SVG** —— 同一句话，deepseek 10 秒能画出 6~9KB、上色准确、结构完整的 SVG

**整个「坐标由模型算、程序只负责渲染」的架构，以及「画过的进画册、可网页查看编辑导出」的产品设计，都是 kana-kwi 的原作。** 本插件只是把「图元指令」换成了「SVG」，并加上了蜡笔质感后处理、人格注入、画谁判定。

也感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 框架提供了插件系统、WebUI bridge 和人格管理。

---

## 许可

AGPL-3.0

---

## 更新日志

### v1.0.0

- 模型写 SVG + cairosvg 渲染 + PIL 蜡笔后处理
- 画册浏览、按关键词翻旧画
- 签名与日期
- 24 项全可配置
- WebUI（测试画图 / 配置 / 画册 / 说明）+ 缩略图缓存
- 人格感知：对话场景注入当前人格，WebUI 测试不注入
- 画谁判定：画自己 vs 画别人
