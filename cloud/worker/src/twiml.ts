/** Tiny TwiML builder. Every interpolated value is XML-escaped. */

/** Escape text content (quotes are fine there, so "Zozo's" stays readable). */
export function escapeXml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function escapeAttr(s: string): string {
  return escapeXml(s).replace(/"/g, "&quot;").replace(/'/g, "&apos;");
}

type Attrs = Record<string, string | number | boolean | undefined>;

function attrs(a: Attrs): string {
  return Object.entries(a)
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => ` ${k}="${escapeAttr(String(v))}"`)
    .join("");
}

export function say(text: string, voice: string): string {
  return `<Say${attrs({ voice })}>${escapeXml(text)}</Say>`;
}

export function pause(seconds: number): string {
  return `<Pause${attrs({ length: seconds })}/>`;
}

export function play(url: string): string {
  return `<Play>${escapeXml(url)}</Play>`;
}

export function gather(a: Attrs, ...inner: string[]): string {
  return `<Gather${attrs(a)}>${inner.join("")}</Gather>`;
}

export function record(a: Attrs): string {
  return `<Record${attrs(a)}/>`;
}

export function redirect(url: string): string {
  return `<Redirect method="POST">${escapeXml(url)}</Redirect>`;
}

export function hangup(): string {
  return "<Hangup/>";
}

export function twiml(...verbs: string[]): string {
  return `<?xml version="1.0" encoding="UTF-8"?><Response>${verbs.join("")}</Response>`;
}

export function twimlResponse(xml: string): Response {
  return new Response(xml, { headers: { "content-type": "text/xml; charset=utf-8" } });
}
