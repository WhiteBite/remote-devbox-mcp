import type { Plugin } from "@opencode-ai/plugin"

const ENV_MAP: ReadonlyArray<readonly [string, string]> = [
  ["DEVBOX_MCP_URL", "MCP_URL"],
  ["DEVBOX_MCP_TOKEN", "MCP_TOKEN"],
  ["DEVBOX_RUNNER_URL", "MCP_RUNNER_URL"],
  ["DEVBOX_RUNNER_TOKEN", "MCP_RUNNER_TOKEN"],
]

const SECRET_PATH = /(^|[\\/])(\.env(\..*)?|[^\\/]+\.(pem|key|p12))$/i
const MUTATING_TOOL = /^(write|edit|apply_patch|bash|webfetch)$/
const BRIDGE_SERVER = "devbox-bridge"

function injectedEnv(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [from, to] of ENV_MAP) {
    const value = process.env[from]
    if (value) out[to] = value
  }
  return out
}

async function registerBridge(client: Parameters<Plugin>[0]["client"]): Promise<void> {
  const url = process.env.DEVBOX_MCP_URL
  const token = process.env.DEVBOX_MCP_TOKEN
  if (!url || !token) {
    throw new Error("DEVBOX_MCP_URL / DEVBOX_MCP_TOKEN are not set")
  }
  const adapter = process.env.DEVBOX_ADAPTER_PATH ?? "arena/mcp-stdio-adapter.py"
  // python3 отсутствует в PATH на Windows
  const python = process.platform === "win32" ? "python" : "python3"
  const trust = process.env.DEVBOX_READONLY === "1" ? "--readonly" : "--trust"
  const result = await client.mcp.add({
    body: {
      name: BRIDGE_SERVER,
      config: {
        type: "local",
        command: [python, adapter, trust],
        environment: { MCP_URL: url, MCP_TOKEN: token },
      },
    },
  })
  if (result.error) {
    throw new Error(`POST /mcp rejected: ${JSON.stringify(result.error)}`)
  }
}

export const DevboxPlugin: Plugin = async ({ client }) => {
  const injected = injectedEnv()
  const readonly = process.env.DEVBOX_READONLY === "1"
  const prefix = process.env.DEVBOX_SERVER_PREFIX ?? ""
  if (process.env.DEVBOX_REGISTER_MCP === "1") {
    // POST /mcp фенсится до готовности инстанса: без await, иначе бутстрап зависает
    void registerBridge(client).catch((error: unknown) => {
      const reason = error instanceof Error ? error.message : String(error)
      const message = `devbox plugin: ${BRIDGE_SERVER} registration failed (${reason}); env injection still active, wire arena/mcp-config.example.json manually`
      void client.app.log({ body: { service: "devbox-plugin", level: "warn", message } }).catch(() => {
        console.error(message)
      })
    })
  }
  return {
    "shell.env": async (_input, output) => {
      if (output?.env && Object.keys(injected).length > 0) {
        Object.assign(output.env, injected)
      }
    },
    "tool.execute.before": async (input, output) => {
      const tool = input.tool ?? ""
      if (readonly && prefix && tool.startsWith(prefix) && MUTATING_TOOL.test(tool.slice(prefix.length))) {
        throw new Error(`devbox plugin: ${tool} is blocked in DEVBOX_READONLY mode`)
      }
      if (tool !== "read") return
      const target = String(output?.args?.filePath ?? output?.args?.path ?? "")
      if (SECRET_PATH.test(target)) {
        throw new Error(`devbox plugin: refused to read secret file ${target}`)
      }
    },
  }
}