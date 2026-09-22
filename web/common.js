/* Multi-Service Invoker 共用的工具与渲染逻辑 */

/* ------------------------------------------------------------ 核心地址 --
 * 网页端是「独立外壳」，核心（multi_invoker 服务）可以在本机，也可以在另一台机器上。
 * 连接地址按下面的优先级解析，解析结果以「路径前缀」的形式拼到所有接口请求上：
 *
 *   1. URL 上的 ?api=http://host:8765  —— 临时指定并记住，方便直接把链接发给同事
 *      ?api=                          —— 显式清空，回到「同源」
 *   2. localStorage["multiInvoker.apiBase"]   —— 上一次用过的核心
 *   3. window.MULTI_INVOKER_API_BASE           —— 独立部署时在页面里写死的默认核心
 *   4. 都没有                          —— 同源（页面就是核心自己发出来的）
 *
 * 空字符串代表「同源」，此时请求仍是相对路径，不做任何跨域。
 */
const API_BASE = (() => {
  const norm = value => String(value == null ? "" : value).trim().replace(/\/+$/, "");
  const params = new URLSearchParams(location.search);
  if(params.has("api")){
    const given = norm(params.get("api"));
    if(given) localStorage.setItem("multiInvoker.apiBase", given);
    else localStorage.removeItem("multiInvoker.apiBase");
  }
  const saved = norm(localStorage.getItem("multiInvoker.apiBase"));
  if(saved) return saved;
  return norm(window.MULTI_INVOKER_API_BASE);
})();

/* 把接口路径拼成完整地址；已经是绝对地址的原样返回。 */
function apiUrl(path){
  const text = String(path == null ? "" : path);
  if(/^https?:\/\//i.test(text)) return text;
  return API_BASE + (text.startsWith("/") ? text : "/" + text);
}

/* 当前连接的核心地址（同源时显示当前站点） */
function apiLabel(){
  if(API_BASE) return API_BASE;
  return location.origin && location.origin !== "null" ? location.origin + "（同源）" : "同源";
}

/* 切换核心：写进 localStorage 并刷新页面，避免半途换源导致状态错乱。 */
function switchApiBase(base){
  const norm = String(base == null ? "" : base).trim().replace(/\/+$/, "");
  if(norm) localStorage.setItem("multiInvoker.apiBase", norm);
  else localStorage.removeItem("multiInvoker.apiBase");
  location.reload();
}

/* 顶部显示当前连接的核心；点一下可以换地址。所有页面共用。 */
function mountCoreBadge(){
  if(document.getElementById("coreBadge")) return;
  const host = document.querySelector("header nav") || document.querySelector("header");
  if(!host) return;
  const badge = document.createElement("button");
  badge.type = "button";
  badge.id = "coreBadge";
  badge.className = "core-badge";
  badge.title = "点击切换要调用的核心地址";
  badge.textContent = API_BASE ? `核心 ${API_BASE}` : "核心 同源";
  badge.onclick = () => {
    const next = window.prompt("核心地址（留空 = 同源）", API_BASE);
    if(next === null) return;
    switchApiBase(next);
  };
  host.append(badge);
}

if(document.readyState === "loading"){
  document.addEventListener("DOMContentLoaded", mountCoreBadge);
}else{
  mountCoreBadge();
}

const STATUS_LABEL = {ok:"成功", http_error:"HTTP错误", network_error:"网络失败",
                      config_error:"配置缺失", preview:"预览"};
const MODULE_SOURCE_LABEL = {
  "service.moduleByPlatform": "调用项按节点指定",
  "service.defaultModule": "调用项默认模块",
};

function escapeHtml(text){
  // 兜底：万一把对象/数组传进来，也不要渲染成 [object Object]
  if(text && typeof text === "object") text = JSON.stringify(text);
  return String(text == null ? "" : text).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function short(text, limit){
  text = String(text == null ? "" : text).replace(/\s+/g, " ");
  limit = limit || 90;
  return text.length > limit ? text.slice(0, limit) + "…" : text;
}

/* 应用上下文单元格：取「匹配到的 URL 规则」里 context 的实际取值。
   规则写死（如某个固定字符串）→ 显示该值，并注明来自哪条规则；
   规则写 <module> → 显示调用项配的应用模块，并注明来源。 */
function contextCell(row){
  const ctx = row.context;
  if(!ctx) return `<span style="color:#8a94a6">未配置</span>`;
  const src = row.contextSource || "";
  const label = src.indexOf("rule:") === 0
    ? `规则：${src.slice(5)}`
    : (MODULE_SOURCE_LABEL[src] || src);
  return `<code>${escapeHtml(ctx)}</code> <span style="color:#8a94a6;font-size:12px">${escapeHtml(label)}</span>`;
}
const moduleCell = contextCell;   // 兼容旧调用名

/* 按行生成 curl 命令，方便人工核对 / 直接拿去 Apifox 或终端验证 */
function curlOf(row){
  const lines = [`curl --location --request ${row.method} '${row.url}'`];
  const headers = row.headers || {};
  const keys = Object.keys(headers);
  if(keys.length && !headers["Content-Type"] && row.requestBody){
    lines[0] += " \\";
    lines.push(`--header 'Content-Type: application/json' \\`);
  }
  keys.forEach((k, i) => {
    const last = (i === keys.length - 1) && !row.requestBody;
    lines.push(`--header '${k}: ${headers[k]}'${last ? "" : " \\"}`);
  });
  if(row.requestBody){
    lines.push(`--data-raw '${String(row.requestBody).replace(/'/g, "'\\''")}'`);
  }
  return lines.join("\n");
}

/* 渲染「将要发出的请求」表格；rows 为 /api/preview 返回的 results */
function renderPlan(container, rows, options){
  options = options || {};
  if(!rows || !rows.length){
    container.innerHTML = `<div class="empty">没有匹配到的节点。</div>`;
    return;
  }
  const body = rows.map((r, i) => {
    if(r.status === "config_error"){
      return `<tr class="clickable" data-i="${i}">
          ${options.multiMode ? `<td><span class="svc-chip">${escapeHtml(r.serviceCode || "")}</span></td>` : ""}
          <td>${escapeHtml(r.platformCode)}</td>
          <td>${escapeHtml(r.platformName || "")}</td>
          <td>${(r.platformTags || []).map(tg => `<span class="tag">${escapeHtml(tg)}</span>`).join("") || "-"}</td>
          <td>${escapeHtml(r.endpointLabel || "")}</td>
          <td>-</td>
          <td>${moduleCell(r)}</td>
          <td class="mono" style="color:#8a94a6">${escapeHtml(r.error || "—")}</td>
          <td>-</td>
        </tr>
        <tr class="detail" id="p${i}" hidden><td colspan="${options.multiMode ? 9 : 8}">
          <div style="color:#8a94a6">${escapeHtml(r.error || "")}</div>
          <div class="legend">请在「调用项定义」页面为该调用项补上对应节点的应用模块。</div>
        </td></tr>`;
    }
    return `<tr class="clickable" data-i="${i}">
        ${options.multiMode ? `<td><span class="svc-chip">${escapeHtml(r.serviceCode || "")}${r.serviceName ? " — " + escapeHtml(r.serviceName) : ""}</span></td>` : ""}
        <td>${escapeHtml(r.platformCode)}</td>
        <td>${escapeHtml(r.platformName || "")}</td>
        <td>${(r.platformTags || []).map(tg => `<span class="tag">${escapeHtml(tg)}</span>`).join("") || "-"}</td>
        <td>${escapeHtml(r.endpointLabel || "")}</td>
        <td>${escapeHtml(r.method || "")}</td>
        <td>${moduleCell(r)}</td>
        <td class="mono">${escapeHtml(r.url || "")}</td>
        <td class="mono">${escapeHtml(short(r.requestBody || "（无请求体）", 60))}</td>
      </tr>
      <tr class="detail" id="p${i}" hidden><td colspan="${options.multiMode ? 9 : 8}">
        <div class="mono" style="margin-bottom:6px">${escapeHtml(r.method)} ${escapeHtml(r.url)}</div>
        ${r.note ? `<div class="note">${escapeHtml(r.note)}</div>` : ""}
        ${Object.keys(r.headers || {}).length ? `<div class="mono" style="margin-bottom:6px">${Object.entries(r.headers).map(([k, v]) => escapeHtml(`${k}: ${v}`)).join("<br>")}</div>` : ""}
        ${r.requestBody ? `<pre>${escapeHtml(r.requestBody)}</pre>` : ""}
        <div style="margin-top:8px">
          <button class="btn" data-copy="${i}">复制 curl 命令</button>
        </div>
        <pre class="curl" id="c${i}">${escapeHtml(curlOf(r))}</pre>
      </td></tr>`;
  }).join("");

  const svcTh = options.multiMode ? `<th style="width:160px">调用项</th>` : "";
  container.innerHTML = `<div class="planwrap"><table>
      <thead><tr>
        ${svcTh}
        <th>平台编码</th><th>平台名称</th><th>标签</th><th>地址标签</th><th>方法</th>
        <th>应用上下文</th><th>请求地址</th><th>请求体</th>
      </tr></thead><tbody>${body}</tbody></table></div>
    ${options.hint ? `<div class="legend">${options.hint}</div>` : ""}`;

  container.querySelectorAll("tr.clickable").forEach(tr => {
    tr.onclick = e => {
      if(e.target.dataset.copy !== undefined) return;
      const detail = container.querySelector("#p" + tr.dataset.i);
      if(detail) detail.hidden = !detail.hidden;
    };
  });
  container.querySelectorAll("[data-copy]").forEach(btn => {
    btn.onclick = async e => {
      e.stopPropagation();
      const text = container.querySelector("#c" + btn.dataset.copy).textContent;
      try{
        await navigator.clipboard.writeText(text);
        btn.textContent = "已复制";
      }catch(err){
        btn.textContent = "复制失败，请手动选中下方命令";
      }
      setTimeout(() => { btn.textContent = "复制 curl 命令"; }, 2000);
    };
  });
}

/* 通用请求封装：统一处理非 2xx 与错误提示。
 * url 传接口路径即可（如 "/api/query"），会自动拼上当前核心地址。 */
async function postJSON(url, payload){
  if(typeof fetch !== "function"){
    throw new Error("当前环境不支持 fetch，请在浏览器中打开本页面");
  }
  let res;
  try{
    res = await fetch(apiUrl(url), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
  }catch(err){
    throw new Error(`连接不上核心 ${apiLabel()}：${err.message || err}`);
  }
  let data = null;
  const text = await res.text();
  try{ data = JSON.parse(text); }catch(e){ data = null; }
  if(!res.ok){
    throw new Error((data && data.error) || `HTTP ${res.status}：${text.slice(0, 200)}`);
  }
  if(data && data.error) throw new Error(data.error);
  return data;
}

/* 带错误处理的 GET：连不上核心时给出可读的提示，而不是一句 Failed to fetch。 */
async function getJSON(url){
  let res;
  try{
    res = await fetch(apiUrl(url));
  }catch(err){
    throw new Error(`连接不上核心 ${apiLabel()}：${err.message || err}`);
  }
  const text = await res.text();
  let data = null;
  try{ data = JSON.parse(text); }catch(e){ data = null; }
  if(!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}：${text.slice(0, 200)}`);
  if(data && data.error) throw new Error(data.error);
  return data;
}

function showNote(el, html, kind){
  el.className = "banner " + (kind || "ok");
  el.innerHTML = html;
  el.hidden = false;
}

function hideNote(el){ el.hidden = true; }

/* 屏幕顶部居中的轻量提示：固定位置、自动消失、不阻塞操作。
 * 参数 {text, kind?: "ok"|"err"|"warn", duration?: 毫秒}。
 * 文本用 textContent 写入 DOM（避免外部字符串注入，见 R064）。
 * 多次调用会叠堆，新的出现在旧的上方；点 × 立即关闭。
 */
function showToast(opts){
  opts = opts || {};
  let host = document.getElementById("toastHost");
  if(!host){
    host = document.createElement("div");
    host.id = "toastHost";
    host.className = "toast-host";
    document.body.append(host);
  }
  const el = document.createElement("div");
  el.className = "toast " + (opts.kind || "ok");
  el.setAttribute("role", "status");
  const text = document.createElement("span");
  text.className = "toast-text";
  text.textContent = opts.text || "";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "关闭");
  close.textContent = "×";
  el.append(text, close);
  // 新的放最上面
  host.insertBefore(el, host.firstChild);
  const dismiss = () => {
    el.classList.add("toast-leaving");
    setTimeout(() => { el.remove(); }, 180);
  };
  close.onclick = dismiss;
  const ms = opts.duration || 2400;
  if(ms > 0) setTimeout(dismiss, ms);
}
