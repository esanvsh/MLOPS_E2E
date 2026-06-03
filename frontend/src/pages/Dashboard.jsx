import { useEffect, useState } from 'react'
import { TrendingUp, AlertTriangle, Activity, ArrowUpRight } from 'lucide-react'
import { getTransactions } from '../services/api'

const RISK_BADGE = {
  HIGH:   'bg-red-950 text-red-400 border border-red-800',
  MEDIUM: 'bg-yellow-950 text-yellow-400 border border-yellow-800',
  LOW:    'bg-green-950 text-green-400 border border-green-800',
}

function StatCard({ icon: Icon, label, value, sub, color }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 flex items-start gap-4">
      <div className={`w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0 ${color}`}>
        <Icon className="w-5 h-5" />
      </div>
      <div>
        <p className="text-gray-500 text-xs font-medium uppercase tracking-wide">{label}</p>
        <p className="text-2xl font-bold text-gray-100 mt-0.5">{value}</p>
        {sub && <p className="text-gray-500 text-xs mt-0.5">{sub}</p>}
      </div>
    </div>
  )
}

export default function Dashboard() {
  const [transactions, setTransactions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    getTransactions()
      .then(({ data }) => setTransactions(Array.isArray(data) ? data : []))
      .catch((err) => {
        if (err.response?.status !== 401) {
          setError('Could not load transactions.')
        }
      })
      .finally(() => setLoading(false))
  }, [])

  const total = transactions.length
  const fraudCount = transactions.filter((t) => t.risk_level === 'HIGH').length
  const avgScore =
    total > 0
      ? (
          transactions.reduce((sum, t) => sum + (t.fraud_probability ?? 0), 0) / total
        ).toFixed(3)
      : '—'

  const recent = [...transactions]
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))
    .slice(0, 5)

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-bold text-gray-100">Dashboard</h2>
        <p className="text-gray-500 text-sm mt-0.5">Real-time fraud detection overview</p>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard
          icon={Activity}
          label="Total Transactions"
          value={loading ? '…' : total.toLocaleString()}
          sub="all time"
          color="bg-blue-950 text-blue-400"
        />
        <StatCard
          icon={AlertTriangle}
          label="Fraud Detected"
          value={loading ? '…' : fraudCount.toLocaleString()}
          sub={total > 0 ? `${((fraudCount / total) * 100).toFixed(1)}% fraud rate` : 'no data'}
          color="bg-red-950 text-red-400"
        />
        <StatCard
          icon={TrendingUp}
          label="Avg Risk Score"
          value={loading ? '…' : avgScore}
          sub="fraud probability"
          color="bg-purple-950 text-purple-400"
        />
      </div>

      {/* Recent transactions */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl">
        <div className="px-5 py-4 border-b border-gray-800 flex items-center justify-between">
          <h3 className="font-semibold text-gray-100 text-sm">Recent Transactions</h3>
          <a
            href="/transactions"
            className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300 transition-colors"
          >
            View all <ArrowUpRight className="w-3 h-3" />
          </a>
        </div>

        {error && (
          <div className="px-5 py-4 text-sm text-red-400">{error}</div>
        )}

        {!error && !loading && recent.length === 0 && (
          <div className="px-5 py-10 text-center text-gray-600 text-sm">
            No transactions yet.
          </div>
        )}

        {!error && (loading || recent.length > 0) && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                  <th className="px-5 py-3 font-medium">Transaction ID</th>
                  <th className="px-5 py-3 font-medium">Amount</th>
                  <th className="px-5 py-3 font-medium">Risk Score</th>
                  <th className="px-5 py-3 font-medium">Risk Level</th>
                  <th className="px-5 py-3 font-medium">Time</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800">
                {loading
                  ? Array.from({ length: 3 }).map((_, i) => (
                      <tr key={i}>
                        {Array.from({ length: 5 }).map((_, j) => (
                          <td key={j} className="px-5 py-3.5">
                            <div className="h-4 bg-gray-800 rounded animate-pulse w-24" />
                          </td>
                        ))}
                      </tr>
                    ))
                  : recent.map((tx) => (
                      <tr key={tx.transaction_id} className="hover:bg-gray-800/50 transition-colors">
                        <td className="px-5 py-3.5 font-mono text-gray-400 text-xs">
                          {tx.transaction_id?.slice(0, 16)}…
                        </td>
                        <td className="px-5 py-3.5 text-gray-200">
                          ${(tx.amount ?? 0).toFixed(2)}
                        </td>
                        <td className="px-5 py-3.5 text-gray-200">
                          {(tx.fraud_probability ?? 0).toFixed(4)}
                        </td>
                        <td className="px-5 py-3.5">
                          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${RISK_BADGE[tx.risk_level] ?? RISK_BADGE.LOW}`}>
                            {tx.risk_level ?? 'LOW'}
                          </span>
                        </td>
                        <td className="px-5 py-3.5 text-gray-500 text-xs">
                          {tx.created_at ? new Date(tx.created_at).toLocaleString() : '—'}
                        </td>
                      </tr>
                    ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
