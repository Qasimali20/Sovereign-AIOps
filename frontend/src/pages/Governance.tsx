import { useSearchParams } from 'react-router-dom'
import { TabbedPage } from '../components/ui'
import Audit from './Audit'
import Policies from './Policies'
import AgentRuns from './AgentRuns'

const TABS = [{ key: 'audit', label: 'Audit log' }, { key: 'policies', label: 'Policies & safety' }, { key: 'agent', label: 'Agent runs' }]

/** Governance: the record of what happened (audit), the rules that decided it (policies), and every investigation the
 *  agent ran (agent runs), one place. */
export default function Governance() {
  const [params, setParams] = useSearchParams()
  const tab = TABS.some(t => t.key === params.get('tab')) ? params.get('tab')! : 'audit'
  return (
    <TabbedPage title="Governance" tabs={TABS} current={tab} onChange={k => setParams({ tab: k }, { replace: true })}>
      {tab === 'audit' ? <Audit embedded /> : tab === 'agent' ? <AgentRuns embedded /> : <Policies embedded />}
    </TabbedPage>
  )
}
