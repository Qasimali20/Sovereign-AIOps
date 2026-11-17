import { useMemo } from 'react'
import { useFleet } from './queries'

/** Last part of a model id or path: "Qwen/Qwen3-ASR-1.7B" -> "Qwen3-ASR-1.7B". */
export const baseName = (s: string) => s.replace(/\/+$/, '').split('/').pop() || s

/** Served name -> complete model name, from what each server actually loaded (fleet data). Served aliases such as
 *  "qwen-embed" or "qwen-chat" say less than "Qwen3-Embedding-0.6B" or "Qwen3-30B-A3B-Instruct-2507-FP8". */
export function useModelNames(): (served: string) => string {
  const { data } = useFleet()
  const map = useMemo(() => {
    const m = new Map<string, string>()
    for (const n of Object.values(data ?? {}))
      for (const s of n.servers ?? [])
        if (s.full_name) { m.set(s.name, s.full_name); m.set(baseName(s.name), s.full_name) }
    return m
  }, [data])
  return (served: string) => map.get(served) ?? map.get(baseName(served)) ?? baseName(served).toLowerCase()
}

/** Does the complete name say something the served name does not? */
export const differs = (full: string, served: string) => full.toLowerCase() !== baseName(served).toLowerCase()

/** The complete model name. The name the server answers to (used in API calls) is in the hover title when it differs. */
export function ModelName({ name, className }: { name: string; inline?: boolean; className?: string }) {
  const full = useModelNames()(name)
  return <span className={className} title={differs(full, name) ? `API name: ${name}` : undefined}>{full}</span>
}
