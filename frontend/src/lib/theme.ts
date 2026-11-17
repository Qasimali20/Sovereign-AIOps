/** Theme preference: light | dark | system. Stored per browser; 'system' follows the OS live.
 * index.html applies the stored preference before first paint, so there is no flash. */
import { useEffect, useState } from 'react'

export type ThemePref = 'light' | 'dark' | 'system'
const KEY = 'aiops.theme'
const media = () => window.matchMedia('(prefers-color-scheme: dark)')

export function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : 'system'
  } catch {
    return 'system'
  }
}

function apply(pref: ThemePref) {
  const dark = pref === 'dark' || (pref === 'system' && media().matches)
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
}

export function useTheme() {
  const [pref, setPref] = useState<ThemePref>(readPref)
  const [systemDark, setSystemDark] = useState(() => media().matches)
  useEffect(() => {
    apply(pref)
    try { localStorage.setItem(KEY, pref) } catch { /* private mode: still applies for this session */ }
  }, [pref, systemDark])
  useEffect(() => {
    const m = media()
    const on = () => setSystemDark(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
  const resolved: 'light' | 'dark' = pref === 'dark' || (pref === 'system' && systemDark) ? 'dark' : 'light'
  return { pref, setPref, resolved }
}

/** The theme actually applied (watches data-theme), so canvas/SVG charts can re-resolve their colours. */
export function useAppliedTheme(): string {
  const [t, setT] = useState(() => document.documentElement.dataset.theme ?? 'light')
  useEffect(() => {
    const mo = new MutationObserver(() => setT(document.documentElement.dataset.theme ?? 'light'))
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => mo.disconnect()
  }, [])
  return t
}
