import type { Register } from 'claude-code'

// Shows "сегодня $X · месяц $Y" in the status line, read from the collector's summary.json.
// The collector (ccstats.py) writes it; this mod only reads a small file, once a minute and after each turn.

type Summary = {
  today: { cost: number }
  period: { cost: number; days: number }
  last_collect: number
}

const money = (x: number) => (x >= 100 ? `$${Math.round(x).toLocaleString('en')}` : `$${x.toFixed(2)}`)

export const register: Register = on => {
  let dir = ''
  let port = 8777

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const refresh = async ($: any) => {
    if (!dir) return
    const text: string = await $.fs.read(`${dir}/summary.json`).then(String).catch(() => '')
    if (!text) {
      $.ui.status('статистика: нет данных')
      return
    }
    try {
      const s = JSON.parse(text) as Summary
      const now = (await $.clock.now()) / 1000
      const age = Math.round((now - s.last_collect) / 60)
      $.ui.status(
        `сегодня ${money(s.today.cost)} · месяц ${money(s.period.cost)}` +
          (age >= 60 ? ` · ${Math.round(age / 60)} ч назад` : ''),
      )
    } catch {
      /* file mid-write: next tick */
    }
  }

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    const home = ((await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? '.').replace(/\\/g, '/')
    dir = ((await $.env.get('CCSTATS_DIR')) ?? `${home}/.claude/usage-stats`).replace(/\\/g, '/')
    const cfg = await $.fs.read(`${dir}/config.json`).then(String).catch(() => '')
    try {
      port = (JSON.parse(cfg) as { port?: number }).port ?? port
    } catch {
      /* defaults */
    }
    await $.command.register({ name: 'usage-stats', description: 'Расход Claude Code: ссылка на дашборд' })
    await refresh($)
    $.clock.every(60_000, () => refresh($))
    return r
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    await refresh($)
    return r
  })

  on('command.run', { name: 'usage-stats' }, async () => ({
    text: `Дашборд: http://127.0.0.1:${port}/ (или файл ${dir}/dashboard.html)`,
  }))
}
