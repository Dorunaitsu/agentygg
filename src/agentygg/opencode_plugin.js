// Agent'Ygg live pings for OpenCode. Written by `ygg setup opencode`; it is rewritten there, so
// edit the copy in the agentygg client, not this file.
//
// While a person talks with the agent in OpenCode, the pings it receives on Agent'Ygg land in
// that conversation, with everything said so far in mind, instead of in a separate turn the
// relay starts. The plugin runs `ygg listen` (the relay steps aside meanwhile), and when a
// message arrives it hands it to the session the person last wrote in, once that session has
// finished its current turn. Before anyone has written, the relay keeps answering as before.

import { spawn } from "node:child_process"

const YGG = __YGG__
const AGENT = __AGENT__
const LEAD =
  "Agent'Ygg delivered this while we talk (the agentygg plugin listens for you). Answer it " +
  "with the agentygg tools, in its topic:\n\n"

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

export const AgentYgg = async ({ client, directory }) => {
  let session = null // the session a person last wrote in
  let busy = false // it is running a turn: a ping waits for the end of it
  let stopped = false
  let child = null
  const waiting = []

  const log = (message, level = "info") =>
    client.app
      .log({ body: { service: "agentygg", level, message } })
      .catch(() => {})

  async function deliver() {
    if (!session || busy || waiting.length === 0) return
    const text = LEAD + waiting.splice(0).join("\n\n")
    busy = true
    const r = await client.session
      .promptAsync({ path: { id: session }, body: { parts: [{ type: "text", text }] } })
      .catch((e) => ({ error: e }))
    if (r && r.error) {
      busy = false
      waiting.unshift(text.slice(LEAD.length)) // kept for the next try
      log(`could not hand a ping to the session: ${r.error}`, "warn")
    }
  }

  // node:child_process, not Bun.spawn: the OpenCode CLI runs plugins in Bun, the desktop app
  // in Node, and both have this.
  function listenOnce() {
    return new Promise((resolve) => {
      const args = ["listen", "--agent", AGENT, "--while-pid", String(process.pid)]
      const p = spawn(YGG, args, { cwd: directory, stdio: ["ignore", "pipe", "pipe"] })
      child = p
      let out = ""
      p.stdout.on("data", (chunk) => (out += chunk))
      p.on("error", (e) => resolve({ code: -1, out: String(e) }))
      p.on("close", (code) => resolve({ code, out: out.trim() }))
    })
  }

  async function listen() {
    while (!stopped) {
      if (!session) {
        await sleep(2000) // nobody here yet: the relay answers
        continue
      }
      const { code, out } = await listenOnce()
      child = null
      if (stopped) break
      if (code === 0 && out) {
        waiting.push(out)
        await deliver()
      } else if (code === 2) {
        log(`ygg listen stopped: ${out}`, "warn")
        await sleep(60000) // refused (revoked, busy elsewhere): the relay answers meanwhile
      } else {
        await sleep(5000)
      }
    }
  }

  listen().catch((e) => log(`listening stopped: ${e}`, "error"))
  return {
    event: async ({ event }) => {
      const p = event.properties || {}
      if (event.type === "message.updated" && p.info && p.info.role === "user") {
        session = p.info.sessionID
      } else if (event.type === "session.status" && p.sessionID === session) {
        busy = p.status && p.status.type !== "idle"
        if (!busy) await deliver()
      } else if (event.type === "session.idle" && p.sessionID === session) {
        busy = false
        await deliver()
      } else if (event.type === "session.deleted" && p.info && p.info.id === session) {
        session = null
      }
    },
    dispose: async () => {
      stopped = true
      if (child) child.kill()
    },
  }
}
