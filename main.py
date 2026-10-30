"""简笔画涂鸦插件：LLM 写 SVG → 渲染成蜡笔简笔画"""

import asyncio
import json
import re
import io
import time
from datetime import datetime
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .crayon import apply_crayon
from .render import (
    add_signature,
    extract_svg,
    normalize_svg,
    render_svg_to_png,
    svg_to_data_uri,
)

PLUGIN_NAME = "astrbot_plugin_svg_doodle"


@register(PLUGIN_NAME, "yangtuoor", "简笔画涂鸦：她写 SVG，程序渲染成蜡笔简笔画", "1.0.0")
class SvgDoodlePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))

    # ---------- 配置读取 ----------
    def _cfg(self, key, default=None):
        if isinstance(self.config, dict):
            v = self.config.get(key, default)
        else:
            try:
                v = self.config[key]
            except Exception:
                v = default
        return default if v is None else v

    @property
    def provider_id(self) -> str:
        return str(self._cfg("provider_id", "") or "").strip()

    @property
    def canvas_size(self) -> int:
        try:
            return int(self._cfg("canvas_size", 768))
        except Exception:
            return 768

    @property
    def gallery_dir(self) -> Path:
        raw = str(self._cfg("gallery_dir", "") or "").strip()
        return Path(raw) if raw else self.data_dir

    # 指向「本角色自己」的关键词
    _SELF_KW = ("自己", "本鱼", "本鲸", "鲸鱼娘", "自画像", "画你")
    # 指向「别人」的关键词
    _OTHER_KW = ("用户", "老大", "主人", "对方", "路人", "画个人", "画一个普通人", "第三人")

    def _who_to_draw(self, subject: str) -> str:
        """判断要画谁：self / other / auto"""
        t = (subject or "").lower()
        name = str(self._cfg("character_name", "") or "").lower()
        hit_self = any(k in t for k in self._SELF_KW) or (name and name in t)
        # 「你」单独出现要小心：「你在哪」是问自己在哪，「画你」才是画自己
        hit_other = any(k in t for k in self._OTHER_KW)
        if re.search(r"画\s*[他她]|画\s*那个|画\s*那个\s*人", t):
            hit_other = True
        if re.search(r"画\s*你|给\s*你\s*画|把\s*你\s*画", t):
            hit_self = True
            hit_other = False
        # 「把我画下来」「画我」→ 画的人是我（用户），不是本角色
        if re.search(r"把\s*我\s*画|画\s*我|我\s*的样子|我的样子|画\s*一下\s*我", t):
            hit_self = False
            hit_other = True
        if hit_other and not hit_self:
            return "other"
        if hit_self and not hit_other:
            return "self"
        return "auto"

    async def _get_persona_prompt(self, event=None) -> str:
        """对话场景下取当前会话生效人格的 prompt；WebUI 测试（event=None）返回空串。"""
        if event is None:
            return ""
        mgr = getattr(self.context, "persona_manager", None)
        if mgr is None:
            return ""

        # 1) 优先走官方解析：会话强制人格 > 会话人格 > 全局默认人格
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            platform_name = ""
            try:
                platform_name = event.get_platform_name() or ""
            except Exception:
                platform_name = ""

            provider_settings = {}
            try:
                cfg = self.context.get_config(umo) or {}
                provider_settings = cfg.get("provider_settings", {}) or {}
            except Exception:
                provider_settings = {}

            conv_persona_id = None
            try:
                # 用事件自带的当前会话，直接读它已经绑定的人格
                conv = getattr(event, "conversation", None)
                if conv is not None:
                    conv_persona_id = getattr(conv, "persona_id", None)
                if conv_persona_id is None and umo:
                    conv_mgr = getattr(self.context, "conversation_manager", None)
                    if conv_mgr is not None:
                        # get_conversations(umo) 拿该会话下最近的对话
                        convs = await conv_mgr.get_conversations(unified_msg_origin=umo)
                        if convs:
                            conv_persona_id = getattr(convs[0], "persona_id", None)
            except Exception as exc:
                logger.debug(f"[{PLUGIN_NAME}] 读会话人格失败: {exc}")
                conv_persona_id = None

            _, persona_obj, _, _ = await mgr.resolve_selected_persona(
                umo=umo or platform_name,
                conversation_persona_id=conv_persona_id,
                platform_name=platform_name,
                provider_settings=provider_settings,
            )
            if persona_obj:
                prompt = str(persona_obj.get("prompt") or "")
                if prompt:
                    logger.debug(f"[{PLUGIN_NAME}] 已注入人格: {persona_obj.get('name')}")
                    return prompt
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] resolve_selected_persona 失败: {exc}")

        # 2) 兜底：直接取默认人格
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            p = await mgr.get_default_persona_v3(umo or None)
            return str((p or {}).get("prompt") or "")
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] get_default_persona_v3 失败: {exc}")
            return ""

    def _build_system_prompt(self, subject: str = "", persona_prompt: str = "") -> str:
        """:param subject: 用户想画的内容
        :param persona_prompt: 对话场景下的当前人格 system_prompt（WebUI 测试传空）
        """
        size = self.canvas_size
        name = str(self._cfg("character_name", "角色") or "角色")
        who = self._who_to_draw(subject)

        parts = []

        # 对话场景：人格描述优先，画什么由人格的自我认知决定
        pp = (persona_prompt or "").strip()
        if pp:
            parts += [
                "【当前角色人格（最高优先级，决定角色长什么样、怎么说话）】",
                pp,
                "",
                "注意：下面的外形描述只是参考底稿，如与上面人格冲突，一律以人格描述为准。",
                "",
            ]

        parts += [
            "【画风要求】",
            str(self._cfg("style_desc", "")),
            "",
            "【最关键的硬约束：头和身体必须连在一起】",
            str(self._cfg("body_constraint", "")),
            "",
        ]

        # 关键：角色外形只在「画本角色」时才注入，避免画用户时把本角色画上去
        if who == "self":
            parts += [
                f"【{name}外形（本次要画的就是{name}本人）】",
                str(self._cfg("character_desc", "")),
                "",
            ]
        elif who == "other":
            parts += [
                "【人物说明】",
                "本次要画的是**用户或其他人**，不是上面那种拟人角色。",
                "请画一个普通人：圆脑袋、两个圆点眼睛、弧线笑脸、梯形或矩形身体、两条竖线当腿、两只小椭圆当鞋。",
                "禁止出现：鲸鱼鳍耳朵、鲸鱼尾巴、鱼鳞、女仆围裙、白色蕾丝头饰、呆毛。",
                "只需要普通人物的发型和衣服区分即可。",
                "",
            ]
        else:
            parts += [
                f"【{name}外形（仅在画面需要{name}时使用）】",
                str(self._cfg("character_desc", "")),
                "",
                "重要：如果画的内容里出现「用户」「老大」「主人」「他」「她」等第三方人物，"
                "这些第三方必须画成**普通人**（不要套用上面的外形），"
                f"上面的外形只属于{name}本人。",
                "",
            ]

        parts += [
            "【输出要求】",
            str(self._cfg("output_suffix", "")).replace("{size}", str(size)),
        ]
        return "\n".join(parts)

    # ---------- 画册 ----------
    def _gallery_file(self) -> Path:
        return self.gallery_dir / "gallery.json"

    def _load_gallery(self) -> dict:
        f = self._gallery_file()
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"items": []}

    def _save_gallery(self, data: dict):
        self.gallery_dir.mkdir(parents=True, exist_ok=True)
        limit = int(self._cfg("gallery_limit", 300))
        items = data.get("items", [])
        if len(items) > limit:
            data["items"] = items[-limit:]
        self._gallery_file().write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- LLM 调用 ----------
    async def _get_provider(self, event: AstrMessageEvent | None = None):
        pid = self.provider_id
        if pid == "__custom__":
            return None
        if pid:
            p = None
            try:
                p = await self.context.get_provider_by_id(pid) if asyncio.iscoroutinefunction(
                    self.context.get_provider_by_id
                ) else self.context.get_provider_by_id(pid)
            except Exception:
                p = None
            if p is None:
                try:
                    p = self.context.provider_manager.get_provider_by_id(pid)
                except Exception:
                    p = None
            if p is not None:
                return p
        try:
            if event is not None:
                return self.context.get_using_provider(event.unified_msg_origin)
            return self.context.get_using_provider()
        except Exception:
            return None

    # ---------- 自定义 OpenAI 兼容调用 ----------
    async def _ask_custom(self, prompt: str, system_prompt: str) -> str | None:
        """直接用 aiohttp 打 OpenAI 兼容端点，用于 AstrBot 未配置的模型。"""
        base = str(self._cfg("custom_api_base", "") or "").strip().rstrip("/")
        model = str(self._cfg("custom_model_name", "") or "").strip()
        key = str(self._cfg("custom_api_key", "") or "").strip()
        if not base or not model:
            logger.warning(f"[{PLUGIN_NAME}] 自定义模型缺少 api_base 或 model_name")
            return None
        url = base + "/chat/completions" if not base.endswith("/chat/completions") else base
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        extra = str(self._cfg("custom_extra_body", "") or "").strip()
        if extra:
            try:
                import json as _json

                more = _json.loads(extra)
                if isinstance(more, dict):
                    payload.update(more)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] custom_extra_body 不是合法 JSON，已忽略: {exc}")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(url, json=payload, headers=headers) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        logger.warning(
                            f"[{PLUGIN_NAME}] 自定义模型 HTTP {resp.status}: {raw[:300]}"
                        )
                        return None
                    data = raw
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 自定义模型请求失败: {exc}")
            return None
        try:
            import json as _json

            obj = _json.loads(data)
            return (obj["choices"][0]["message"]["content"] or "").strip() or None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 自定义模型响应解析失败: {exc}")
            return None

    async def _ask_llm(self, prompt: str, event: AstrMessageEvent | None = None) -> str | None:
        # 对话场景取当前人格；WebUI 测试（event=None）不注入人格
        persona_prompt = await self._get_persona_prompt(event)
        sys_prompt0 = self._build_system_prompt(prompt, persona_prompt)
        if self.provider_id == "__custom__":
            return await self._ask_custom(prompt, sys_prompt0)
        provider = await self._get_provider(event)
        if provider is None:
            logger.warning(f"[{PLUGIN_NAME}] 没有可用的模型提供商")
            return None
        sys_prompt = sys_prompt0
        try:
            resp = await provider.text_chat(
                prompt=prompt, system_prompt=sys_prompt, contexts=[]
            )
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] LLM 调用失败: {exc}")
            return None
        text = getattr(resp, "completion_text", None) or ""
        if not text and isinstance(resp, str):
            text = resp
        return text or None

    # ---------- 核心：画一张 ----------
    async def draw(
        self,
        subject: str,
        event: AstrMessageEvent | None = None,
    ) -> dict:
        """画一张图，返回 {ok, path, svg, error, elapsed}"""
        t0 = time.time()
        size = self.canvas_size
        prefix = str(self._cfg("scene_prefix", "") or "")
        prompt = f"{prefix}{subject}" if prefix else subject
        max_retry = int(self._cfg("max_retry", 2))

        gdir = self.gallery_dir
        (gdir / "images").mkdir(parents=True, exist_ok=True)

        last_err = ""
        for attempt in range(max_retry + 1):
            text = await self._ask_llm(prompt, event)
            if not text:
                last_err = "模型无响应"
                continue
            svg = extract_svg(text)
            if not svg:
                last_err = "没有找到 SVG 代码"
                if attempt < max_retry:
                    prompt = (
                        f"{subject}\n\n"
                        "上一次的输出里没有可用的 SVG。请只输出完整的 <svg>...</svg> 代码，"
                        "不要任何解释文字，不要 markdown 代码块。"
                    )
                continue
            svg = normalize_svg(svg, size)
            ts = int(time.time() * 1000)
            raw_png = gdir / "images" / f"raw_{ts}.png"
            if not render_svg_to_png(svg, str(raw_png), size):
                last_err = "SVG 渲染失败"
                if attempt < max_retry:
                    prompt = (
                        f"{subject}\n\n"
                        "上一次的 SVG 语法有问题渲染失败了。请检查 path 的 d 属性格式，"
                        "只输出合法的最简 SVG。"
                    )
                continue

            final_png = gdir / "images" / f"doodle_{ts}.png"
            try:
                apply_crayon(
                    str(raw_png),
                    str(final_png),
                    size=size,
                    texture_strength=float(self._cfg("texture_strength", 0.0)),
                    edge_jitter=float(self._cfg("edge_jitter", 1.8)),
                    grain_strength=float(self._cfg("grain_strength", 14.0)),
                    stroke_rough=float(self._cfg("stroke_rough", 1.0)),
                )
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 后处理失败，用原图: {exc}")
                final_png = raw_png

            add_signature(
                str(final_png),
                signature=str(self._cfg("signature", "") or ""),
                show_date=bool(self._cfg("show_date", True)),
                font_path=str(self._cfg("font_path", "") or ""),
                date_text=datetime.now().strftime("%Y.%m.%d"),
                size=size,
            )

            try:
                raw_png.unlink(missing_ok=True)
            except Exception:
                pass

            gal = self._load_gallery()
            gal.setdefault("items", []).append(
                {
                    "subject": subject,
                    "time": ts,
                    "file": str(final_png.name),
                    "attempts": attempt + 1,
                }
            )
            self._save_gallery(gal)

            return {
                "ok": True,
                "path": str(final_png),
                "svg": svg,
                "error": "",
                "elapsed": round(time.time() - t0, 1),
            }

        return {
            "ok": False,
            "path": "",
            "svg": "",
            "error": last_err or "未知错误",
            "elapsed": round(time.time() - t0, 1),
        }

    # ---------- 权限检查 ----------
    def _allowed(self, event: AstrMessageEvent) -> bool:
        groups = self._cfg("enabled_groups", []) or []
        if event.is_private_chat():
            return bool(self._cfg("allow_private", True))
        gid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        if not groups:
            return True
        return gid in [str(g) for g in groups]

    # ---------- LLM 工具 ----------
    @filter.llm_tool(name="draw_doodle")
    async def draw_doodle_tool(self, event: AstrMessageEvent, subject: str):
        """画一张简笔画涂鸦。

        Args:
            subject(string): 要画的内容描述，比如「坐在礁石上看海」「戴着耳机听歌」
        """
        if not self._allowed(event):
            yield event.plain_result("这里不让画哦")
            return
        result = await self.draw(subject, event)
        if not result["ok"]:
            logger.warning(f"[{PLUGIN_NAME}] 画图失败: {result['error']}")
            yield event.plain_result(f"画失败了：{result['error']}")
            return
        yield event.chain_result(
            [Comp.Image.fromFileSystem(result["path"])]
        )

    @filter.llm_tool(name="recall_doodle")
    async def recall_doodle_tool(self, event: AstrMessageEvent, keyword: str = ""):
        """翻出之前画过的涂鸦发出去。

        Args:
            keyword(string): 关键词，用来匹配画过的主题。留空则发最近一张
        """
        gal = self._load_gallery()
        items = gal.get("items", [])
        if not items:
            yield event.plain_result("画册还是空的")
            return
        kw = (keyword or "").strip()
        matched = [it for it in items if kw and kw in str(it.get("subject", ""))]
        target = (matched or items)[-1]
        path = self.gallery_dir / "images" / str(target.get("file", ""))
        if not path.is_file():
            yield event.plain_result("那张图找不到了")
            return
        yield event.chain_result([Comp.Image.fromFileSystem(str(path))])

    # ---------- 指令 ----------
    @filter.command("涂鸦", alias={"画图", "画画"})
    async def doodle_cmd(self, event: AstrMessageEvent, subject: str = ""):
        """画一张简笔画。用法：/涂鸦 坐在礁石上看海"""
        if not self._allowed(event):
            yield event.plain_result("这里不让画哦")
            return
        if not subject.strip():
            yield event.plain_result("要画什么？比如 /涂鸦 坐在礁石上看海")
            return
        yield event.plain_result("画着呢…")
        result = await self.draw(subject, event)
        if not result["ok"]:
            yield event.plain_result(f"画失败了：{result['error']}")
            return
        yield event.chain_result([Comp.Image.fromFileSystem(result["path"])])

    # ---------- WebUI API ----------
    async def initialize(self):
        self._register_apis()
        logger.info(f"[{PLUGIN_NAME}] 已加载；画布={self.canvas_size} 签名={self._cfg('signature')}")

    async def terminate(self):
        logger.info(f"[{PLUGIN_NAME}] 已卸载")

    def _register_apis(self):
        reg = self.context.register_web_api
        routes = [
            ("page/status", self.page_status, ["GET"], "插件状态"),
            ("page/models", self.page_models, ["GET"], "可用模型列表"),
            ("page/config", self.page_get_config, ["GET"], "读取配置"),
            ("page/config/save", self.page_save_config, ["POST"], "保存配置"),
            ("page/test", self.page_test, ["POST"], "测试画图"),
            ("page/gallery", self.page_gallery, ["GET"], "画册列表"),
            ("page/preview", self.page_preview, ["GET"], "预览图片"),
        ]
        for path, handler, methods, desc in routes:
            try:
                reg(f"/{PLUGIN_NAME}/{path}", handler, methods, desc)
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 注册 {path} 失败: {exc}")

    async def _json_body(self) -> dict:
        try:
            from quart import request

            data = await request.get_json(force=True, silent=True)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _args(self) -> dict:
        try:
            from quart import request

            return dict(request.args)
        except Exception:
            return {}

    def _ok(self, data=None):
        return {"status": "ok", "data": data or {}}

    def _err(self, msg: str):
        return {"status": "error", "message": msg}

    async def page_models(self):
        """列出 AstrBot 已配置的对话模型，供前端下拉选择。"""
        bad_id = ("embedding", "_stt", "_tts", "rerank", "asr", "speech")
        bad_type = ("embedding", "stt", "asr", "tts", "rerank", "speech")
        items = []

        # 1) 读 cmd_config.json 的 providers（最权威）
        try:
            import json as _json
            from pathlib import Path as _P

            candidates = [
                _P("/opt/astrbot/data/cmd_config.json"),
                self.data_dir.parent.parent / "cmd_config.json",
            ]
            cfg_path = next((c for c in candidates if c.is_file()), None)
            if cfg_path is not None:
                cfg = _json.load(io.open(cfg_path, encoding="utf-8-sig"))
                for p in cfg.get("provider", []) or []:
                    pid = str(p.get("id", "") or "").strip()
                    ptype = str(p.get("type", "") or "").lower()
                    if not pid:
                        continue
                    if p.get("enable") is False:
                        continue
                    if any(t in pid.lower() for t in bad_id):
                        continue
                    if any(t in ptype for t in bad_type):
                        continue
                    if "chat_completion" not in ptype:
                        continue
                    items.append({"id": pid, "label": pid})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 读取 cmd_config 失败: {exc}")

        # 2) 兜底：provider_manager 的实例表（同样过滤）
        if not items:
            try:
                pm = getattr(self.context, "provider_manager", None)
                insts = getattr(pm, "inst_map", None) or {}
                if isinstance(insts, dict):
                    for pid in insts.keys():
                        pid = str(pid or "").strip()
                        if not pid or any(t in pid.lower() for t in bad_id):
                            continue
                        items.append({"id": pid, "label": pid})
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 读取 provider_manager 失败: {exc}")

        seen = set()
        uniq = []
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            uniq.append(it)
        uniq.sort(key=lambda x: x["id"])
        uniq.insert(0, {"id": "", "label": "跟随当前会话默认模型"})
        uniq.append({"id": "__custom__", "label": "自定义（下面填 API 地址 + 模型名）"})
        return self._ok({"items": uniq})

    async def page_status(self):
        gal = self._load_gallery()
        provider = await self._get_provider()
        return self._ok(
            {
                "plugin": PLUGIN_NAME,
                "canvas_size": self.canvas_size,
                "provider_id": self.provider_id,
                "provider_available": provider is not None,
                "provider_name": getattr(provider, "meta", lambda: None)() and "",
                "gallery_count": len(gal.get("items", [])),
                "signature": self._cfg("signature", ""),
                "character_name": self._cfg("character_name", ""),
            }
        )

    async def page_get_config(self):
        schema_file = Path(__file__).parent / "_conf_schema.json"
        schema = {}
        if schema_file.is_file():
            try:
                schema = json.loads(schema_file.read_text(encoding="utf-8"))
            except Exception:
                schema = {}
        values = {}
        for k in schema:
            values[k] = self._cfg(k, schema[k].get("default"))
        return self._ok({"schema": schema, "values": values})

    async def page_save_config(self):
        body = await self._json_body()
        patch = body.get("values") if isinstance(body.get("values"), dict) else body
        if not isinstance(patch, dict):
            return self._err("参数格式错误")
        schema_file = Path(__file__).parent / "_conf_schema.json"
        schema = {}
        if schema_file.is_file():
            try:
                schema = json.loads(schema_file.read_text(encoding="utf-8"))
            except Exception:
                schema = {}
        changed = 0
        for k, v in patch.items():
            if k not in schema:
                continue
            try:
                self.config[k] = v
                changed += 1
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 写入 {k} 失败: {exc}")
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 保存配置失败: {exc}")
        logger.info(f"[{PLUGIN_NAME}] 配置已更新 {changed} 项")
        return self._ok({"changed": changed})

    async def page_test(self):
        body = await self._json_body()
        subject = str(body.get("subject") or "").strip()
        if not subject:
            return self._err("请填写要画的内容")
        result = await self.draw(subject, None)
        if not result["ok"]:
            return self._err(result["error"])
        return self._ok(
            {
                "subject": subject,
                "file": Path(result["path"]).name,
                "elapsed": result["elapsed"],
                "svg": result["svg"],
            }
        )

    async def page_gallery(self):
        gal = self._load_gallery()
        items = list(reversed(gal.get("items", [])))
        return self._ok({"items": items, "total": len(items)})

    def _thumb(self, src: Path, width: int) -> Path:
        """生成并缓存缩略图（JPEG q=82）。失败时回退原图。"""
        width = max(64, min(int(width), 1536))
        cache_dir = self.gallery_dir / "thumbs"
        dst = cache_dir / f"{src.stem}_w{width}.jpg"
        try:
            if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
                return dst
            from PIL import Image

            cache_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im:
                im = im.convert("RGB")
                if im.width > width:
                    h = max(1, round(im.height * width / im.width))
                    im = im.resize((width, h), Image.LANCZOS)
                im.save(dst, "JPEG", quality=82, optimize=True)
            return dst
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 生成缩略图失败，回退原图: {exc}")
            return src

    async def page_preview(self):
        """返回图片。默认返回 base64 data URI（JSON），?raw=1 时返回二进制 PNG。

        支持 ?w=480 缩略图：按需缩小并转 JPEG，体积约为原图 1/30，
        画册网格一次加载 30 张也不会卡。
        """
        args = await self._args()
        name = str(args.get("file", "") or "")
        if not name or "/" in name or ".." in name:
            return self._err("文件名非法")
        path = self.gallery_dir / "images" / name
        if not path.is_file():
            return self._err("文件不存在")

        # 缩略图：?w=0 表示原图，否则按给定宽度生成缓存
        try:
            want_w = int(args.get("w", 0) or 0)
        except (TypeError, ValueError):
            want_w = 0
        if want_w > 0:
            path = self._thumb(path, want_w)

        # raw=1：直接返回 PNG 二进制（供 <img src> 使用，但需同源鉴权）
        if str(args.get("raw", "") or "") in ("1", "true", "True"):
            try:
                from quart import Response

                data = path.read_bytes()
                mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
                return Response(data, mimetype=mime)
            except Exception as exc:  # noqa: BLE001
                return self._err(f"读取失败: {exc}")

        # 默认：base64 data URI，走 bridge.apiGet 即可拿到，不依赖 assetUrl
        try:
            import base64

            data = path.read_bytes()
            mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            return self._ok(
                {
                    "file": name,
                    "mime": mime,
                    "size": len(data),
                    "data_uri": "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(f"读取失败: {exc}")
