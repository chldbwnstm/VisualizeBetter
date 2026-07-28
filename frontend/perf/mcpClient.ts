/**
 * Minimal MCP client for the [15] performance harness.
 *
 * Deliberately separate from e2e/graph.spec.ts's copy: that suite is the stable
 * 13-test functional gate and this measurement must not be able to perturb it.
 */

export const BASE = process.env.VISUALIZEBETTER_URL ?? 'http://127.0.0.1:8790'
const MCP = `${BASE}/mcp/`

let session: string | null = null

async function rpc(method: string, params: unknown, id = 1): Promise<string> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    accept: 'application/json, text/event-stream',
  }
  if (session) headers['mcp-session-id'] = session

  const response = await fetch(MCP, {
    method: 'POST',
    headers,
    body: JSON.stringify({ jsonrpc: '2.0', id, method, params }),
  })
  const issued = response.headers.get('mcp-session-id')
  if (issued) session = issued
  return response.text()
}

export async function connectMcp(): Promise<void> {
  session = null
  await rpc('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'perf', version: '1' },
  })
  await fetch(MCP, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      accept: 'application/json, text/event-stream',
      ...(session ? { 'mcp-session-id': session } : {}),
    },
    body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }),
  })
}

let nextId = 1

/** Unwrap a Streamable-HTTP tool response (SSE framing → JSON-RPC → tool value). */
function parseToolResult(body: string): Record<string, unknown> {
  const line = body.split('\n').find((l) => l.startsWith('data:'))
  const message = JSON.parse(line ? line.slice(5).trim() : body)
  const result = message.result
  if (result?.structuredContent) return result.structuredContent
  const text = result?.content?.[0]?.text
  return text ? JSON.parse(text) : result
}

/** Throws on failure: a silently dropped push would flatter every number here. */
export async function callTool(
  name: string,
  args: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const body = await rpc('tools/call', { name, arguments: args }, nextId++)
  if (!body.includes('"result"') || body.includes('"isError":true')) {
    throw new Error(`MCP ${name} failed: ${body.slice(0, 200)}`)
  }
  return parseToolResult(body)
}

export interface NodeSpec {
  id: string
  label: string
  type: string
}

export function nodeSpecs(prefix: string, from: number, count: number): NodeSpec[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `${prefix}.${from + i}`,
    label: `n${from + i}`,
    type: 'class',
  }))
}
