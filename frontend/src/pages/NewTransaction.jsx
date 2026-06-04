import { useState, useRef, useEffect } from 'react'
import { createTransaction, getTransaction } from '../services/api'

const CATEGORIES = [
  { value: 'electronics', label: 'Electronics' },
  { value: 'grocery', label: 'Grocery' },
  { value: 'travel', label: 'Travel' },
  { value: 'entertainment', label: 'Entertainment' },
  { value: 'food', label: 'Food' },
  { value: 'healthcare', label: 'Healthcare' },
  { value: 'other', label: 'Other' },
]

const COUNTRIES = [
  { value: 'IN', label: '🇮🇳 India' },
  { value: 'US', label: '🇺🇸 USA' },
  { value: 'GB', label: '🇬🇧 UK' },
  { value: 'SG', label: '🇸🇬 Singapore' },
  { value: 'AE', label: '🇦🇪 UAE' },
]

const PAYMENT_METHODS = [
  { value: 'credit_card', label: 'Credit Card', icon: '💳' },
  { value: 'debit_card', label: 'Debit Card', icon: '🏦' },
  { value: 'upi',         label: 'UPI',         icon: '📱' },
  { value: 'neft',        label: 'NEFT',        icon: '🔄' },
  { value: 'imps',        label: 'IMPS',        icon: '⚡' },
]

const RESULT_CONFIG = {
  HIGH: {
    icon: '⚠️',
    message: 'Transaction Flagged — HIGH RISK',
    cardCls: 'bg-red-950/60 border-red-700',
    textCls: 'text-red-300',
    badgeCls: 'bg-red-900 text-red-300',
  },
  MEDIUM: {
    icon: '🔍',
    message: 'Under Review — MEDIUM RISK',
    cardCls: 'bg-yellow-950/60 border-yellow-700',
    textCls: 'text-yellow-300',
    badgeCls: 'bg-yellow-900 text-yellow-300',
  },
  LOW: {
    icon: '✅',
    message: 'Approved — LOW RISK',
    cardCls: 'bg-green-950/60 border-green-700',
    textCls: 'text-green-300',
    badgeCls: 'bg-green-900 text-green-300',
  },
  UNKNOWN: {
    icon: '⏳',
    message: 'Result Pending — Still Processing',
    cardCls: 'bg-gray-800 border-gray-700',
    textCls: 'text-gray-300',
    badgeCls: 'bg-gray-700 text-gray-400',
  },
}

const BLANK = {
  amount: '',
  merchant_category: 'electronics',
  country: 'IN',
  payment_method: 'upi',
  device_id: '',
}

function ResultCard({ result, onReset }) {
  const cfg = RESULT_CONFIG[result.risk_level] ?? RESULT_CONFIG.UNKNOWN
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-5">
      <div className={`border rounded-xl p-5 ${cfg.cardCls}`}>
        <div className="flex items-start gap-4">
          <span className="text-4xl leading-none mt-0.5">{cfg.icon}</span>
          <div>
            <p className={`text-lg font-bold ${cfg.textCls}`}>{cfg.message}</p>
            <p className="text-gray-500 text-sm mt-1">
              Transaction ID:{' '}
              <span className="font-mono text-gray-400">{result.transaction_id}</span>
            </p>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <div className="bg-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">Fraud Probability</p>
          <p className={`text-2xl font-bold ${cfg.textCls}`}>
            {result.fraud_probability != null
              ? `${(result.fraud_probability * 100).toFixed(1)}%`
              : '—'}
          </p>
        </div>
        <div className="bg-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">Risk Level</p>
          <span className={`inline-block mt-1.5 px-2.5 py-1 rounded-full text-xs font-semibold ${cfg.badgeCls}`}>
            {result.risk_level}
          </span>
        </div>
        <div className="bg-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">Model Version</p>
          <p className="text-sm font-mono text-gray-300 mt-1.5">{result.model_version ?? '—'}</p>
        </div>
      </div>

      <button
        onClick={onReset}
        className="w-full bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 font-medium py-3 px-4 rounded-lg text-sm transition-colors"
      >
        Submit Another Transaction
      </button>
    </div>
  )
}

export default function NewTransaction() {
  const [form, setForm] = useState({ ...BLANK })
  const [phase, setPhase] = useState('form') // form | processing | result
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const pollRef = useRef(null)

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setPhase('processing')

    let txId
    try {
      const { data } = await createTransaction({
        amount: parseFloat(form.amount),
        merchant_category: form.merchant_category,
        country: form.country,
        payment_method: form.payment_method,
        device_id: form.device_id || '',
        ip_address: '',
      })
      txId = data.transaction_id
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to submit transaction.')
      setPhase('form')
      return
    }

    let polls = 0
    pollRef.current = setInterval(async () => {
      polls++
      try {
        const { data } = await getTransaction(txId)
        const scored =
          data.risk_level !== 'UNKNOWN' && data.fraud_prediction !== 'PENDING'
        if (scored || polls >= 12) {
          clearInterval(pollRef.current)
          setResult(data)
          setPhase('result')
        }
      } catch {
        clearInterval(pollRef.current)
        setError('Failed to retrieve transaction status.')
        setPhase('form')
      }
    }, 2000)
  }

  const reset = () => {
    setForm({ ...BLANK })
    setResult(null)
    setError('')
    setPhase('form')
  }

  const inputCls =
    'w-full bg-gray-800 border border-gray-700 text-gray-100 placeholder-gray-600 px-4 py-3 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition'
  const selectCls = inputCls + ' appearance-none cursor-pointer'
  const labelCls = 'block text-xs font-medium text-gray-400 uppercase tracking-wide mb-2'

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h2 className="text-xl font-bold text-gray-100">New Transaction</h2>
        <p className="text-gray-500 text-sm mt-0.5">
          Submit a payment for real-time fraud analysis
        </p>
      </div>

      {error && (
        <div className="bg-red-950 border border-red-800 text-red-300 px-4 py-3 rounded-lg text-sm">
          {error}
        </div>
      )}

      {phase === 'form' && (
        <form
          onSubmit={handleSubmit}
          className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-6"
        >
          {/* Amount */}
          <div>
            <label className={labelCls}>Amount</label>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 font-medium pointer-events-none select-none">
                ₹
              </span>
              <input
                type="number"
                min="1"
                max="100000"
                step="0.01"
                required
                value={form.amount}
                onChange={(e) => setForm({ ...form, amount: e.target.value })}
                placeholder="0.00"
                className={inputCls + ' pl-7'}
              />
            </div>
          </div>

          {/* Merchant Category */}
          <div>
            <label className={labelCls}>Merchant Category</label>
            <select
              value={form.merchant_category}
              onChange={(e) => setForm({ ...form, merchant_category: e.target.value })}
              className={selectCls}
            >
              {CATEGORIES.map(({ value, label }) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>

          {/* Country */}
          <div>
            <label className={labelCls}>Country</label>
            <select
              value={form.country}
              onChange={(e) => setForm({ ...form, country: e.target.value })}
              className={selectCls}
            >
              {COUNTRIES.map(({ value, label }) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>

          {/* Payment Method */}
          <div>
            <label className={labelCls}>Payment Method</label>
            <div className="grid grid-cols-5 gap-2">
              {PAYMENT_METHODS.map(({ value, label, icon }) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setForm({ ...form, payment_method: value })}
                  className={`flex flex-col items-center gap-1.5 px-2 py-3 rounded-lg border text-xs font-medium transition-all ${
                    form.payment_method === value
                      ? 'border-blue-500 bg-blue-600/20 text-blue-300'
                      : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600 hover:text-gray-200'
                  }`}
                >
                  <span className="text-xl">{icon}</span>
                  {label}
                </button>
              ))}
            </div>
          </div>

          {/* Device ID */}
          <div>
            <label className={labelCls}>
              Device ID{' '}
              <span className="text-gray-600 normal-case font-normal">(optional)</span>
            </label>
            <input
              type="text"
              value={form.device_id}
              onChange={(e) => setForm({ ...form, device_id: e.target.value })}
              placeholder="e.g. device-abc123"
              className={inputCls}
            />
          </div>

          <button
            type="submit"
            className="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-3 px-4 rounded-lg text-sm transition-colors"
          >
            Submit Transaction
          </button>
        </form>
      )}

      {phase === 'processing' && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-14 flex flex-col items-center gap-5">
          <div className="w-14 h-14 border-4 border-blue-600/30 border-t-blue-500 rounded-full animate-spin" />
          <div className="text-center">
            <p className="text-gray-100 font-semibold text-lg">Processing payment...</p>
            <p className="text-gray-500 text-sm mt-1">Analysing transaction for fraud signals</p>
          </div>
        </div>
      )}

      {phase === 'result' && result && (
        <ResultCard result={result} onReset={reset} />
      )}
    </div>
  )
}
