import type { Plugin } from "@opencode-ai/plugin"

const ENV_MAP: ReadonlyArray<readonly [string, string]> = [
  ["DEVBOX_MCP_URL", "MCP_URL"],
  ["DEVBOX_MCP_TOKEN", "MCP_TOKEN"],
  ["DEVBOX_RUNNER_URL", "MCP_RUNNER_URL"],
  ["DEVBOX_RUNNER_TOKEN", "MCP_RUNNER_TOKEN"],
]

const SECRET_PATH = /(^|[\\/])(\.env(\..*)?|[^\\/]+\.(pem|key|p12))$/i

function injectedEnv(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [from, to] of ENV_MAP) {
    const value = process.env[from]
    if (value) out[to] = value
  }
  return out
}

export const DevboxPlugin: Plugin = async () => {
  const injected = injectedEnv()
  return {
    "shell.env": async (_input, output) => {
      if (output?.env && Object.keys(injected).length > 0) {
        Object.assign(output.env, injected)
      }
    },
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "read") return
      const target = String(output?.args?.filePath ?? output?.args?.path ?? "")
      if (SECRET_PATH.test(target)) {
        throw new Error(`devbox plugin: refused to read secret file ${target}`)
      }
    },
  }
}