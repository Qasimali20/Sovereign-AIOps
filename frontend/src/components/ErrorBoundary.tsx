import { Component, type ErrorInfo, type ReactNode } from 'react'

/** Keeps a failing page from blanking the whole app: the shell stays usable and the error is shown in place. */
export default class ErrorBoundary extends Component<{ children: ReactNode; resetKey: string }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('page error', error, info.componentStack)
  }

  componentDidUpdate(prev: { resetKey: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null })
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <main className="content">
        <div className="callout crit">
          <div>
            <b>This page could not be displayed.</b>
            <div className="small" style={{ marginTop: 4 }}>{this.state.error.message}</div>
            <button className="btn btn-sm" style={{ marginTop: 10 }} onClick={() => this.setState({ error: null })}>Try again</button>
          </div>
        </div>
      </main>
    )
  }
}
