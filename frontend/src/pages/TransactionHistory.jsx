import { useState, useEffect, Fragment } from 'react'
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import { getTransactions } from '../services/api'

const RISK_BADGE = {
  HIGH:    'bg-red-950 text-red-400 border border-red-800',
  MEDIUM:  'bg-yellow-950 text-yellow-400 border border-yellow-800',
  LOW:     'bg-green-950 text-green-400 border border-green-800',
  UNKNOWN: 'bg-gray-800 text-gray-500 border border-gray-700',
  PENDING: 'bg-gray-800 text-gray-500 border border-gray-700',
}

const fmt    = (v) => v ?? '—'
const fmtPct = (v) => v != null ? `${(Number(v) * 100).toFixed(1)}%` : '—'
const fmtAmt = (v) =>
  v != null
    ? `₹${Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : '—'
const fmtDate = (v) => (v ? new Date(v).toLocaleString() : '—')
const fmtMethod = (v) => v?.replace(/_/g, ' ') ?? '—'

function Detail({ label, value }) {
  return (
    <div>
      <p className="text-xs text-gray-500 uppercase tracking-wide mb-0.5">{label}</p>
      <p className="text-gray-300 text-sm">{value}</p>
    </div>
  )
}

export default function TransactionHistory() {
  const [transactions, setTransactions] = useState([])
  const [loading, setLoading]     = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError]         = useState('')
  const [expandedId, setExpandedId] = useState(null)

  const load = async (silent = false) => {
    silent ? setRefreshing(true) : setLoading(true)
    try {
      const { data } = await getTransactions()
      setTransactions(Array.isArray(data) ? data : [])
      setError('')
    } catch (err) {
      if (err.response?.status !== 401) setError('Failed to load transactions.')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => {
    load()
    const interval = setInterval(() => load(true), 10_000)
    return () => clearInterval(interval)
  }, [])

  const toggle = (id) => setExpandedId((prev) => (prev === id ? null : id))

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold text-gray-100">Transaction History</h2>
          <p className="text-gray-500 text-sm mt-0.5">Auto-refreshes every 10 seconds</p>
        </div>
        <button
          onClick={() => load(true)}
          disabled={refreshing}
          className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg text-xs text-gray-400 transition-colors disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {error && (
        <div className="bg-red-950 border border-red-800 text-red-300 px-4 py-3 rounded-lg text-sm">
          {error}
        </div>
      )}

      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        {/* Loading skeleton */}
        {loading && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <TableHead />
              </thead>
              <tbody className="divide-y divide-gray-800">
                {Array.from({ length: 5 }).map((_, i) => (
                  <tr key={i}>
                    {Array.from({ length: 8 }).map((_, j) => (
                      <td key={j} className="px-5 py-3.5">
                        <div className="h-4 bg-gray-800 rounded animate-pulse w-20" />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Empty state */}
        {!loading && !error && transactions.length === 0 && (
          <div className="px-5 py-14 text-center text-gray-600 text-sm">
            No transactions yet.{' '}
            <a href="/transactions/new" className="text-blue-400 hover:text-blue-300">
              Submit one.
            </a>
          </div>
        )}

        {/* Table */}
        {!loading && transactions.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <TableHead />
              </thead>
              <tbody className="divide-y divide-gray-800">
                {transactions.map((tx) => (
                  <Fragment key={tx.transaction_id}>
                    <tr
                      onClick={() => toggle(tx.transaction_id)}
                      className="hover:bg-gray-800/50 cursor-pointer transition-colors"
                    >
                      <td className="pl-4 pr-2 py-3.5 text-gray-500 w-8">
                        {expandedId === tx.transaction_id ? (
                          <ChevronDown className="w-4 h-4" />
                        ) : (
                          <ChevronRight className="w-4 h-4" />
                        )}
                      </td>
                      <td className="px-4 py-3.5 text-gray-400 text-xs whitespace-nowrap">
                        {fmtDate(tx.created_at)}
                      </td>
                      <td className="px-4 py-3.5 text-gray-200 font-medium whitespace-nowrap">
                        {fmtAmt(tx.amount)}
                      </td>
                      <td className="px-4 py-3.5 text-gray-400 capitalize">
                        {fmt(tx.merchant_category)}
                      </td>
                      <td className="px-4 py-3.5 text-gray-400">
                        {fmt(tx.country)}
                      </td>
                      <td className="px-4 py-3.5 text-gray-400 capitalize">
                        {fmtMethod(tx.payment_method)}
                      </td>
                      <td className="px-4 py-3.5">
                        <span
                          className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                            RISK_BADGE[tx.risk_level] ?? RISK_BADGE.UNKNOWN
                          }`}
                        >
                          {tx.risk_level ?? 'UNKNOWN'}
                        </span>
                      </td>
                      <td className="px-4 py-3.5 text-gray-500 text-xs">
                        {fmt(tx.status)}
                      </td>
                    </tr>

                    {expandedId === tx.transaction_id && (
                      <tr className="bg-gray-800/30 border-b border-gray-700">
                        <td colSpan={8} className="px-6 py-5">
                          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                            <Detail
                              label="Transaction ID"
                              value={
                                <span className="font-mono text-xs break-all">
                                  {tx.transaction_id}
                                </span>
                              }
                            />
                            <Detail label="Amount" value={fmtAmt(tx.amount)} />
                            <Detail
                              label="Fraud Probability"
                              value={fmtPct(tx.fraud_probability)}
                            />
                            <Detail label="Fraud Prediction" value={fmt(tx.fraud_prediction)} />
                            <Detail
                              label="Payment Method"
                              value={
                                <span className="capitalize">{fmtMethod(tx.payment_method)}</span>
                              }
                            />
                            <Detail label="Country" value={fmt(tx.country)} />
                            <Detail label="Model Version" value={fmt(tx.model_version)} />
                            <Detail label="Created At" value={fmtDate(tx.created_at)} />
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function TableHead() {
  return (
    <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
      <th className="pl-4 pr-2 py-3 w-8" />
      <th className="px-4 py-3 font-medium">Date / Time</th>
      <th className="px-4 py-3 font-medium">Amount</th>
      <th className="px-4 py-3 font-medium">Category</th>
      <th className="px-4 py-3 font-medium">Country</th>
      <th className="px-4 py-3 font-medium">Method</th>
      <th className="px-4 py-3 font-medium">Risk Level</th>
      <th className="px-4 py-3 font-medium">Status</th>
    </tr>
  )
}
