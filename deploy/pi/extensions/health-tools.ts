/**
 * 健康档案 —— 智能体工具白名单扩展
 *
 * 这个扩展定义了智能体「能做的全部事情」。不在这里注册的能力，模型就没有工具可用，
 * 因此内置的 read / write / edit / bash / grep / find / ls 等一律不在其可及范围内。
 *
 * 允许范围（与用户商定一致）：
 *   1. 当前病情询问      → health_context / record_query
 *   2. 病情问答          → kb_search + record_query
 *   3. 月经相关询问与预测 → record_query(kind=cycle_stats) / health_context
 *   4. 数据记录整理归档   → submit_record（只提交「提案」，必须人工批准后才落库）
 *   5. 联网搜索          → web_search / web_fetch（先过服务端隐私围栏与域名白名单）
 *
 * 明确不提供：执行命令、读写文件、git 操作、直接写库、删除数据、访问白名单外域名。
 *
 * 环境变量：
 *   BG_AGENT_URL    本系统地址，默认 http://127.0.0.1:9810
 *   BG_AGENT_TOKEN  作用域令牌（仅 root 可读的环境文件里提供）
 *   BG_SEARCH_URL   搜索服务地址（Tavily 或 SearXNG；未配置则联网搜索不可用）
 *   BG_SEARCH_KEY   Tavily 等服务的 API key
 *   BG_SEARCH_KIND  tavily | searxng（默认 tavily）
 */

const BASE = process.env.BG_AGENT_URL || "http://127.0.0.1:9810";  // 由 deploy/pi/run.sh 注入
const TOKEN = process.env.BG_AGENT_TOKEN || "";
const SEARCH_URL = process.env.BG_SEARCH_URL || "";
const SEARCH_KEY = process.env.BG_SEARCH_KEY || "";
const SEARCH_KIND = (process.env.BG_SEARCH_KIND || "tavily").toLowerCase();

const ALLOWED_TOOLS = [
  "health_context",
  "record_query",
  "kb_search",
  "web_search",
  "web_fetch",
  "submit_record",
  "list_pending",
];

async function api(path: string, init: any = {}): Promise<any> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Agent-Token": TOKEN,
      ...(init.headers || {}),
    },
  });
  const text = await res.text();
  let data: any;
  try {
    data = JSON.parse(text);
  } catch {
    data = { raw: text.slice(0, 500) };
  }
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : JSON.stringify(data).slice(0, 300);
    throw new Error(`${res.status} ${detail}`);
  }
  return data;
}

function ok(text: string, details: any = {}) {
  return { content: [{ type: "text", text }], details };
}

/** 粗略把 HTML 变成可读文本（不做渲染，够用即可） */
function htmlToText(html: string): string {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<nav[\s\S]*?<\/nav>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
}

export default function (pi: any) {
  // 第一层：会话开始即把可用工具收敛到白名单（内置文件/命令工具全部关掉）
  pi.on("session_start", async (_event: any, _ctx: any) => {
    const before = typeof pi.getActiveTools === "function" ? pi.getActiveTools() : [];
    if (typeof pi.setActiveTools === "function") {
      pi.setActiveTools(ALLOWED_TOOLS);
    }
    try {
      await api("/api/agent/audit", {
        method: "POST",
        body: JSON.stringify({
          tool: "session_start",
          decision: "allowed",
          detail: `内置工具已收敛：之前 ${JSON.stringify(before)}`,
        }),
      });
    } catch {
      /* 审计失败不影响会话 */
    }
  });

  pi.registerTool({
    name: "health_context",
    label: "档案概览",
    description:
      "读取档案主人当前的概览：月经周期统计与下次经期推断、进行中的疾病、近期就诊与日志摘要。" +
      "回答任何与「她现在的身体状况」有关的问题前，先调用这个工具。",
    parameters: { type: "object", properties: {}, required: [] },
    async execute() {
      const d = await api("/api/agent/context");
      return ok(JSON.stringify(d, null, 2), d);
    },
  });

  pi.registerTool({
    name: "record_query",
    label: "记录查询",
    description:
      "按类型查询归档的原始记录（只读）。kind 可选：" +
      "cycle_stats（周期统计与预测）、cycle_history（历次月经）、conditions（疾病列表）、" +
      "condition_detail（某疾病及其病程，需要 id）、visits（就诊记录）、day_logs（每日日志）、" +
      "search（按关键词在记录中搜索，需要 q）。",
    parameters: {
      type: "object",
      properties: {
        kind: { type: "string", description: "查询类型，见工具说明" },
        id: { type: "integer", description: "condition_detail 时必填：疾病档案 id" },
        q: { type: "string", description: "search 时必填：关键词" },
      },
      required: ["kind"],
    },
    async execute(_id: string, params: any) {
      const d = await api("/api/agent/query", {
        method: "POST",
        body: JSON.stringify({ kind: params.kind, payload: { id: params.id, q: params.q } }),
      });
      return ok(JSON.stringify(d, null, 2), d);
    },
  });

  pi.registerTool({
    name: "kb_search",
    label: "知识库检索",
    description:
      "在本机医学知识库中检索（含月经周期生理、异常判定标准、常见妇科疾病、检查与参考值、就医指引）。" +
      "回答病情或月经问题时，凡涉及医学判断，必须先用它找依据，并在回答里引用文件名。",
    parameters: {
      type: "object",
      properties: { q: { type: "string", description: "检索关键词，如 月经过多 / 痛经 / 排卵" } },
      required: ["q"],
    },
    async execute(_id: string, params: any) {
      const d = await api("/api/agent/kb/search", {
        method: "POST",
        body: JSON.stringify({ q: params.q }),
      });
      const hits = (d.结果 || d.results || []).slice(0, 5);
      if (!hits.length) {
        return ok(`知识库未命中「${params.q}」。可以换更短的关键词，或明确告知用户知识库暂无该主题。`);
      }
      const text = hits
        .map((h: any) => `【${h.title} — ${h.section}】docs/${h.rel}\n${h.snippet}`)
        .join("\n\n");
      return ok(text, d);
    },
  });

  pi.registerTool({
    name: "web_search",
    label: "联网检索",
    description:
      "在互联网上检索权威医学资料（用于补充知识库未收录的问题）。" +
      "**只能发送通用医学术语**：不得包含任何个人信息（姓名、账号、域名、邮箱）或具体日期。" +
      "查询会先经过服务端隐私围栏，被拒绝时请改写为通用词再试。",
    parameters: {
      type: "object",
      properties: { q: { type: "string", description: "通用医学检索词，例如「月经过多 诊断标准」" } },
      required: ["q"],
    },
    async execute(_id: string, params: any) {
      const guard = await api("/api/agent/guard", {
        method: "POST",
        body: JSON.stringify({ kind: "search", text: params.q }),
      }).catch((e: any) => ({ allowed: false, reason: String(e.message || e) }));
      if (!guard.allowed) {
        return ok(`联网检索被隐私围栏拒绝：${guard.reason}。请改用不含个人信息的通用检索词。`);
      }
      if (!SEARCH_URL) {
        return ok(
          "联网检索未配置：需要设置 BG_SEARCH_URL（推荐 Tavily，免费 1,000 次/月，无需信用卡）。" +
            "在此之前请基于知识库与档案数据回答，并说明无法联网。",
        );
      }
      let results: any[] = [];
      if (SEARCH_KIND === "searxng") {
        const r = await fetch(
          `${SEARCH_URL.replace(/\/$/, "")}/search?q=${encodeURIComponent(guard.text)}&format=json`,
          { headers: SEARCH_KEY ? { Authorization: `Bearer ${SEARCH_KEY}` } : {} },
        );
        const j: any = await r.json();
        results = (j.results || []).map((x: any) => ({
          title: x.title,
          url: x.url,
          snippet: (x.content || "").slice(0, 300),
        }));
      } else {
        const r = await fetch(`${SEARCH_URL.replace(/\/$/, "")}/search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            api_key: SEARCH_KEY,
            query: guard.text,
            max_results: 5,
            search_depth: "basic",
          }),
        });
        const j: any = await r.json();
        results = (j.results || []).map((x: any) => ({
          title: x.title,
          url: x.url,
          snippet: (x.content || "").slice(0, 300),
        }));
      }
      try {
        await api("/api/agent/audit", {
          method: "POST",
          body: JSON.stringify({
            tool: "web_search",
            decision: "allowed",
            detail: `已检索：${guard.text}`,
            preview: results.map((r) => r.url).join(" ").slice(0, 200),
          }),
        });
      } catch {}
      if (!results.length) return ok(`未检索到结果：${guard.text}`);
      return ok(
        results.map((r, i) => `${i + 1}. ${r.title}\n   ${r.url}\n   ${r.snippet}`).join("\n\n"),
        { query: guard.text, results },
      );
    },
  });

  pi.registerTool({
    name: "web_fetch",
    label: "打开网页",
    description:
      "抓取指定网页正文（仅限权威医学来源白名单，如 acog.org / msdmanuals.com / who.int / nice.org.uk / ncbi.nlm.nih.gov）。" +
      "用于核实知识库里的说法或补全细节。白名单外的链接会被拒绝。",
    parameters: {
      type: "object",
      properties: { url: { type: "string", description: "要抓取的完整网址" } },
      required: ["url"],
    },
    async execute(_id: string, params: any) {
      const guard = await api("/api/agent/guard", {
        method: "POST",
        body: JSON.stringify({ kind: "fetch", url: params.url }),
      }).catch((e: any) => ({ allowed: false, reason: String(e.message || e) }));
      if (!guard.allowed) return ok(`抓取被拒绝：${guard.reason}`);
      const res = await fetch(guard.url, {
        headers: {
          "User-Agent":
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
          "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
      });
      const html = await res.text();
      const text = htmlToText(html).slice(0, 8000);
      try {
        await api("/api/agent/audit", {
          method: "POST",
          body: JSON.stringify({
            tool: "web_fetch",
            decision: "allowed",
            detail: `HTTP ${res.status}`,
            preview: guard.url,
          }),
        });
      } catch {}
      return ok(`来源：${guard.url}（HTTP ${res.status}）\n\n${text}`, { url: guard.url });
    },
  });

  pi.registerTool({
    name: "submit_record",
    label: "提交录入提案",
    description:
      "把要归档的数据提交为**待批准提案**（不会直接写库）。用于：记录一次月经、一条症状/用药日志、" +
      "一个新的疾病诊断、一条病程事件、一次就诊。kind 可选：cycle_add / day_log / condition_add / " +
      "condition_event / visit_add / condition_update。提交后必须告知用户去「管理 → 智能体权限」批准。" +
      "日期字段用 YYYY-MM-DD；不确定的字段不要编造，留空或先问用户。",
    parameters: {
      type: "object",
      properties: {
        kind: { type: "string", description: "提案类型" },
        payload: { type: "object", description: "字段与值（见系统提示中的字段说明）" },
        rationale: { type: "string", description: "为什么建议记录这条，供用户判断" },
      },
      required: ["kind", "payload"],
    },
    async execute(_id: string, params: any) {
      const d = await api("/api/agent/propose", {
        method: "POST",
        body: JSON.stringify({
          kind: params.kind,
          payload: params.payload || {},
          rationale: params.rationale || "",
        }),
      });
      return ok(
        `已提交为待批准提案 #${d.proposal_id}（状态 pending）。` +
          `请提醒用户到「管理 → 智能体权限」批准后才会写入数据库。`,
        d,
      );
    },
  });

  pi.registerTool({
    name: "list_pending",
    label: "待批准提案",
    description: "查看当前尚未批准的录入提案，避免重复提交同一条数据。",
    parameters: { type: "object", properties: {}, required: [] },
    async execute() {
      const d = await api("/api/agent/proposals");
      const items = d["待批准"] || d.pending || [];
      if (!items.length) return ok("当前没有待批准的提案。");
      return ok(
        items
          .map(
            (p: any) =>
              `#${p.id} ${p.kind_cn}｜${p.created_at}\n   内容：${JSON.stringify(p.payload_obj)}\n   理由：${p.rationale || "—"}`,
          )
          .join("\n"),
        d,
      );
    },
  });
}
