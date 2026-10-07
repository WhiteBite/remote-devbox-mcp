import assert from "node:assert/strict"
import { test } from "node:test"

import { DevboxPlugin } from "./devbox.ts"

function fakeClient(record) {
  return {
    mcp: {
      add: async (arg) => {
        record.push(arg)
        return {}
      },
    },
    app: { log: async () => {} },
  }
}

async function withEnv(vars, fn) {
  const saved = new Map()
  for (const [key, value] of Object.entries(vars)) {
    saved.set(key, process.env[key])
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
  try {
    return await fn()
  } finally {
    for (const [key, value] of saved) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  }
}

test("shell.env injects the devbox env mapping", async () => {
  await withEnv(
    {
      DEVBOX_MCP_URL: "https://x/p/8787/mcp",
      DEVBOX_MCP_TOKEN: "bridge-token",
      DEVBOX_RUNNER_URL: undefined,
      DEVBOX_RUNNER_TOKEN: undefined,
    },
    async () => {
      const hooks = await DevboxPlugin({ client: fakeClient([]) })
      const output = { env: {} }
      await hooks["shell.env"]({}, output)
      assert.equal(output.env.MCP_URL, "https://x/p/8787/mcp")
      assert.equal(output.env.MCP_TOKEN, "bridge-token")
      assert.equal(output.env.MCP_RUNNER_URL, undefined)
    },
  )
})

test("tool.execute.before blocks secret reads", async () => {
  const hooks = await DevboxPlugin({ client: fakeClient([]) })
  await assert.rejects(
    () => hooks["tool.execute.before"]({ tool: "read" }, { args: { filePath: "/w/.env" } }),
    /secret/,
  )
})

test("readonly blocks devbox mutating tools, allows reads", async () => {
  await withEnv({ DEVBOX_READONLY: "1", DEVBOX_SERVER_PREFIX: "devbox-bridge_" }, async () => {
    const hooks = await DevboxPlugin({ client: fakeClient([]) })
    await assert.rejects(
      () => hooks["tool.execute.before"]({ tool: "devbox-bridge_bash" }, { args: {} }),
      /DEVBOX_READONLY/,
    )
    await hooks["tool.execute.before"]({ tool: "devbox-bridge_read" }, { args: {} })
  })
})

test("DEVBOX_REGISTER_MCP registers the bridge via client.mcp.add", async () => {
  await withEnv(
    { DEVBOX_REGISTER_MCP: "1", DEVBOX_MCP_URL: "https://x/p/8787/mcp", DEVBOX_MCP_TOKEN: "t" },
    async () => {
      const record = []
      await DevboxPlugin({ client: fakeClient(record) })
      await new Promise((resolve) => setTimeout(resolve, 30))
      assert.equal(record.length, 1)
      assert.equal(record[0].body.name, "devbox-bridge")
      assert.equal(record[0].body.config.type, "local")
    },
  )
})