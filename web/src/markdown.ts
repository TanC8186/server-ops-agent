import { Marked } from "marked";

function escapeHtml(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function decodeUrlEntities(value: string): string {
  const named: Record<string, string> = { amp: "&", colon: ":", Tab: "\t", NewLine: "\n", quot: '"', apos: "'", lt: "<", gt: ">" };
  return value.replace(/&#(x[\da-f]+|\d+);?|&(amp|colon|Tab|NewLine|quot|apos|lt|gt);/gi, (entity, code: string | undefined, name: string | undefined) => {
    if (code) {
      const point = code[0].toLowerCase() === "x" ? parseInt(code.slice(1), 16) : parseInt(code, 10);
      return point > 0 && point <= 0x10ffff ? String.fromCodePoint(point) : "\ufffd";
    }
    return named[name ?? ""] ?? named[(name ?? "").toLowerCase()] ?? entity;
  });
}

/** Check the decoded scheme and escape the original attribute independently. */
export function safeMarkdownUrl(href: string, image = false): string | null {
  const displayUrl = decodeUrlEntities(href).trim();
  let canonical = displayUrl;
  for (let count = 0; count < 8; count++) {
    const decoded = decodeUrlEntities(canonical).replace(/%([\da-f]{2})/gi, (_match, hex: string) => String.fromCharCode(parseInt(hex, 16)));
    if (decoded === canonical) break;
    canonical = decoded;
    if (count === 7) return null;
  }
  // eslint-disable-next-line no-control-regex -- URL schemes must be checked without whitespace and control characters.
  canonical = canonical.replace(/[\s\u0000-\u001f\u007f-\u009f]/g, "");
  if (canonical.includes("\\")) return null;
  const scheme = /^([a-z][a-z\d+.-]*):/i.exec(canonical)?.[1].toLowerCase();
  if (scheme && !["http", "https", ...(image ? [] : ["mailto"])].includes(scheme)) return null;
  try {
    const parsed = new URL(canonical, "https://oncall.invalid/");
    if (!["http:", "https:", ...(image ? [] : ["mailto:"])].includes(parsed.protocol)) return null;
  } catch {
    return null;
  }
  return displayUrl;
}

const marked = new Marked({
  gfm: true,
  breaks: true,
});

marked.use({
  renderer: {
    html(token) {
      // 报告来自本地 LLM/MCP，仍按纯文本处理，避免注入
      return escapeHtml(token.text);
    },
    link(token) {
      const text = this.parser.parseInline(token.tokens);
      const href = safeMarkdownUrl(token.href);
      if (href === null) return text;
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      return `<a href="${escapeHtml(href)}"${title} rel="noopener noreferrer">${text}</a>`;
    },
    image(token) {
      const text = escapeHtml(token.text);
      const href = safeMarkdownUrl(token.href, true);
      if (href === null) return text;
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      return `<img src="${escapeHtml(href)}" alt="${text}"${title} loading="lazy">`;
    },
  },
});

export function renderMarkdown(source: string): string {
  return marked.parse(source, { async: false }) as string;
}
