/**
 * TVBox 配置托管 Worker
 * 完整说明见 docs/自建服务器.md
 *
 * 需要两个绑定（Worker → Settings → Bindings）：
 *   KV Namespace   TVBOX       存 config / live / spider / class_rules
 *   Secret / Text  UPLOAD_KEY  上传口令
 */

const H_JSON = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
  "access-control-allow-origin": "*",
};
const H_CONFIG = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "public, max-age=30",
  "access-control-allow-origin": "*",
};
const H_M3U = {
  "content-type": "audio/x-mpegurl; charset=utf-8",
  "cache-control": "public, max-age=60",
  "access-control-allow-origin": "*",
};
const H_JAR = {
  "content-type": "application/java-archive",
  "cache-control": "public, max-age=300",
  "access-control-allow-origin": "*",
};
const H_SITE = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "public, max-age=60",
  "access-control-allow-origin": "*",
};
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,POST,OPTIONS",
  "access-control-allow-headers": "*",
};
const UA = "okhttp/3.12.0";

function json(o, status) {
  return new Response(JSON.stringify(o), { status: status || 200, headers: H_JSON });
}

function okKey(url, env) {
  const k = url.searchParams.get("key") || "";
  const want = (env && env.UPLOAD_KEY) || "";
  return want !== "" && k === want;
}

/* ============================ 分类改写 ============================ */

function hitSensitive(name, words) {
  const n = String(name == null ? "" : name);
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    if (w && n.indexOf(w) >= 0) return true;
  }
  return false;
}

function pickKeep(list, key, rules, idOf) {
  const keepId = rules.sites && rules.sites[key] != null ? rules.sites[key] : null;
  if (keepId !== null) {
    for (let i = 0; i < list.length; i++) {
      if (String(idOf(list[i])) === String(keepId)) return list[i];
    }
  }
  // 找不到指定 id 就退而求其次：保留第一个命中的敏感分类，避免「学习」整个消失
  return list[0];
}

function patchJson(text, key, rules) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    return null;
  }
  if (!obj || !Array.isArray(obj.class) || obj.class.length === 0) return null;
  const words = rules.sensitive || [];
  const all = obj.class;
  const bad = all.filter(function (c) {
    return hitSensitive(c.type_name, words);
  });
  if (bad.length === 0) return null;
  const keep = pickKeep(bad, key, rules, function (c) {
    return c.type_id;
  });
  const out = all.filter(function (c) {
    return !hitSensitive(c.type_name, words);
  });
  const last = Object.assign({}, keep);
  last.type_name = rules.rename || "学习";
  out.push(last);
  obj.class = out;
  return JSON.stringify(obj);
}

function patchXml(text, key, rules) {
  const m = text.match(/<class>([\s\S]*?)<\/class>/i);
  if (!m) return null;
  const inner = m[1];
  // 注意：只能匹配 <ty ...>名称</ty>，不能写成 <ty[^>]*>[\s\S]*?</ty>，
  // 否则会从 <type> 一路吃到别处，整个分类列表就没了
  const re = /<ty\s([^>]*)>([^<]*)<\/ty>/gi;
  const ents = [];
  let x;
  while ((x = re.exec(inner)) !== null) {
    ents.push({ attrs: x[1], name: x[2] });
  }
  if (ents.length === 0) return null;
  const words = rules.sensitive || [];
  const idOf = function (e) {
    const a = e.attrs.match(/id\s*=\s*["']?([^"'\s>]+)/i);
    return a ? a[1] : "";
  };
  const bad = ents.filter(function (e) {
    return hitSensitive(e.name, words);
  });
  if (bad.length === 0) return null;
  const keep = pickKeep(bad, key, rules, idOf);
  const out = ents.filter(function (e) {
    return !hitSensitive(e.name, words);
  });
  out.push({ attrs: keep.attrs, name: rules.rename || "学习" });
  const body = out
    .map(function (e) {
      return "<ty " + e.attrs + ">" + e.name + "</ty>";
    })
    .join("");
  return text.replace(m[0], "<class>" + body + "</class>");
}

function patchClass(text, key, rules) {
  if (!text) return null;
  const head = text.length > 6000 ? text.slice(0, 6000) : text;
  const looksXml = /<class>/i.test(head);
  const looksJson = head.indexOf('"class"') >= 0;
  if (!looksXml && !looksJson) return null;
  // 源站偶尔带 BOM，JSON.parse 会直接失败
  const body = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  try {
    return looksXml ? patchXml(body, key, rules) : patchJson(body, key, rules);
  } catch (e) {
    return null;
  }
}

function b64decode(s) {
  let t = String(s || "").replace(/-/g, "+").replace(/_/g, "/");
  while (t.length % 4 !== 0) t += "=";
  try {
    const bin = atob(t);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder("utf-8").decode(bytes);
  } catch (e) {
    return "";
  }
}

async function readRules(env) {
  if (!env || !env.TVBOX) return null;
  try {
    const raw = await env.TVBOX.get("class_rules");
    if (!raw) return null;
    const r = JSON.parse(raw);
    if (!r || !Array.isArray(r.sensitive)) return null;
    return r;
  } catch (e) {
    return null;
  }
}

/* ============================ 代理 ============================ */

// /site/<key>/<base64(原接口地址)>   —— 单站点接口代理 + 分类改写
// 用路径而不是 ?url= 是故意的：老安卓的 TVBox 拼查询参数会出问题
async function handleSite(env, url) {
  const rest = url.pathname.slice("/site/".length);
  const sp = rest.indexOf("/");
  if (sp <= 0) return json({ ok: false, error: "bad site path" }, 400);
  const key = decodeURIComponent(rest.slice(0, sp));
  const upstream = b64decode(rest.slice(sp + 1));
  if (!/^https?:\/\//i.test(upstream)) return json({ ok: false, error: "bad upstream" }, 400);

  let target = upstream;
  const qs = url.search ? url.search.slice(1) : "";
  if (qs) target += (upstream.indexOf("?") >= 0 ? "&" : "?") + qs;

  let resp;
  try {
    resp = await fetch(target, {
      headers: { "user-agent": UA, accept: "*/*" },
      redirect: "follow",
    });
  } catch (e) {
    return json({ ok: false, error: "upstream fail", detail: String(e) }, 502);
  }

  const ct = (resp.headers.get("content-type") || "").toLowerCase();
  const buf = await resp.arrayBuffer();
  const isText =
    ct.indexOf("json") >= 0 || ct.indexOf("xml") >= 0 || ct.indexOf("text") >= 0 || ct === "";
  if (!isText) {
    return new Response(buf, {
      status: resp.status,
      headers: {
        "content-type": ct || "application/octet-stream",
        "cache-control": "public, max-age=120",
        "access-control-allow-origin": "*",
      },
    });
  }

  const text = new TextDecoder("utf-8").decode(buf);
  const rules = await readRules(env);
  if (rules) {
    const patched = patchClass(text, key, rules);
    if (patched) return new Response(patched, { status: 200, headers: H_SITE });
  }
  const outType =
    ct && ct.indexOf("charset") >= 0
      ? ct
      : (ct || "application/json") + "; charset=utf-8";
  return new Response(text, {
    status: resp.status,
    headers: {
      "content-type": outType,
      "cache-control": "public, max-age=60",
      "access-control-allow-origin": "*",
    },
  });
}

// /proxy?url=<编码后的地址>  —— 用来抓 GitHub / jsDelivr 这些常被墙的资源
async function handleProxy(url) {
  const target = url.searchParams.get("url") || "";
  if (!/^https?:\/\//i.test(target)) return json({ ok: false, error: "bad url" }, 400);
  let resp;
  try {
    resp = await fetch(target, { headers: { "user-agent": UA }, redirect: "follow" });
  } catch (e) {
    return json({ ok: false, error: "fetch fail", detail: String(e) }, 502);
  }
  const buf = await resp.arrayBuffer();
  return new Response(buf, {
    status: resp.status,
    headers: {
      "content-type": resp.headers.get("content-type") || "application/octet-stream",
      "cache-control": "public, max-age=300",
      "access-control-allow-origin": "*",
    },
  });
}

/* ============================ 主入口 ============================ */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const p = url.pathname;

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    if (p.indexOf("/site/") === 0) return handleSite(env, url);
    if (p === "/proxy") return handleProxy(url);

    const kv = env && env.TVBOX ? env.TVBOX : null;
    if (!kv) {
      return json({ ok: false, error: "KV 没绑定：Binding 的名字必须是 TVBOX" }, 500);
    }

    /* ---------- 上传 ---------- */

    if (request.method === "POST" && p === "/upload") {
      if (!okKey(url, env)) return json({ ok: false, error: "key error" }, 403);
      const body = await request.text();
      let cfg;
      try {
        cfg = JSON.parse(body);
      } catch (e) {
        return json({ ok: false, error: "bad json" }, 400);
      }
      if (!cfg || !Array.isArray(cfg.sites) || cfg.sites.length === 0) {
        return json({ ok: false, error: "not a tvbox config" }, 400);
      }
      await kv.put("config", body);
      await kv.put("updated", new Date().toISOString());
      return json({
        ok: true,
        sites: cfg.sites.length,
        size: body.length,
        updated: new Date().toISOString(),
      });
    }

    if (request.method === "POST" && p === "/upload-live") {
      if (!okKey(url, env)) return json({ ok: false, error: "key error" }, 403);
      const body = await request.text();
      if (body.trim().indexOf("#EXTM3U") !== 0) {
        return json({ ok: false, error: "not m3u" }, 400);
      }
      await kv.put("live", body);
      await kv.put("live_updated", new Date().toISOString());
      return json({
        ok: true,
        size: body.length,
        channels: (body.match(/#EXTINF/g) || []).length,
        updated: new Date().toISOString(),
      });
    }

    if (request.method === "POST" && p === "/upload-jar") {
      if (!okKey(url, env)) return json({ ok: false, error: "key error" }, 403);
      const buf = new Uint8Array(await request.arrayBuffer());
      if (buf.length < 1000) return json({ ok: false, error: "too small" }, 400);
      const zip = buf[0] === 0x50 && buf[1] === 0x4b && buf[2] === 0x03 && buf[3] === 0x04;
      if (!zip) return json({ ok: false, error: "not a zip/jar" }, 400);
      await kv.put("spider", buf);
      await kv.put("spider_updated", new Date().toISOString());
      return json({ ok: true, size: buf.length, updated: new Date().toISOString() });
    }

    if (request.method === "POST" && p === "/upload-rules") {
      if (!okKey(url, env)) return json({ ok: false, error: "key error" }, 403);
      const body = await request.text();
      let rules;
      try {
        rules = JSON.parse(body);
      } catch (e) {
        return json({ ok: false, error: "bad json" }, 400);
      }
      if (!rules || !Array.isArray(rules.sensitive)) {
        return json({ ok: false, error: "need {sensitive: [...]}" }, 400);
      }
      await kv.put("class_rules", body);
      await kv.put("rules_updated", new Date().toISOString());
      return json({
        ok: true,
        sensitive: rules.sensitive.length,
        sites: Object.keys(rules.sites || {}).length,
        rename: rules.rename || "学习",
        updated: new Date().toISOString(),
      });
    }

    /* ---------- 读取 ---------- */

    if (p === "/tvbox.json" || p === "/api.json") {
      const body = await kv.get("config");
      if (!body) return json({ ok: false, error: "empty" }, 404);
      return new Response(body, { headers: H_CONFIG });
    }

    if (p === "/live.m3u" || p === "/live.txt" || p === "/live") {
      const body = await kv.get("live");
      if (!body) return new Response("#EXTM3U\n", { status: 404, headers: H_M3U });
      return new Response(body, { headers: H_M3U });
    }

    if (p === "/spider.jar" || p === "/jar") {
      const body = await kv.get("spider", "arrayBuffer");
      if (!body) return new Response("no jar", { status: 404, headers: H_JAR });
      return new Response(body, { headers: H_JAR });
    }

    if (p === "/rules") {
      const raw = await kv.get("class_rules");
      if (!raw) return json({ ok: false, error: "no rules" }, 404);
      return new Response(raw, { headers: H_JSON });
    }

    if (p === "/status") {
      const cfg = await kv.get("config");
      const live = await kv.get("live");
      const jar = await kv.get("spider", "arrayBuffer");
      const rules = await kv.get("class_rules");
      let sites = 0;
      let lives = 0;
      let spider = "";
      if (cfg) {
        try {
          const j = JSON.parse(cfg);
          sites = (j.sites || []).length;
          lives = (j.lives || []).length;
          spider = j.spider || "";
        } catch (e) {}
      }
      let ruleSites = 0;
      let ruleWords = 0;
      if (rules) {
        try {
          const r = JSON.parse(rules);
          ruleSites = Object.keys(r.sites || {}).length;
          ruleWords = (r.sensitive || []).length;
        } catch (e) {}
      }
      return json({
        ok: !!cfg,
        sites: sites,
        lives: lives,
        updated: await kv.get("updated"),
        live_ok: !!live,
        live_size: live ? live.length : 0,
        live_channels: live ? (live.match(/#EXTINF/g) || []).length : 0,
        live_updated: await kv.get("live_updated"),
        jar_ok: !!jar,
        jar_size: jar ? jar.byteLength : 0,
        rules_ok: !!rules,
        rules_sites: ruleSites,
        rules_sensitive: ruleWords,
        spider: spider,
      });
    }

    if (p === "/" || p === "") {
      return new Response(
        "TVBox config host\n\n" +
          "GET  /tvbox.json   配置（电视填这个）\n" +
          "GET  /live.m3u     精选直播\n" +
          "GET  /status       状态\n" +
          "GET  /rules        分类改写规则\n" +
          "GET  /site/<key>/<base64>   单站点代理\n" +
          "GET  /proxy?url=   通用代理\n" +
          "POST /upload?key=          上传配置\n" +
          "POST /upload-live?key=     上传 m3u\n" +
          "POST /upload-jar?key=      上传 jar\n" +
          "POST /upload-rules?key=    更新分类规则\n",
        { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } }
      );
    }

    return new Response("not found", {
      status: 404,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
