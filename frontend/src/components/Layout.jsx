import { useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard,
  PlusCircle,
  History,
  BarChart2,
  ShieldCheck,
  LogOut,
  Shield,
  Menu,
  X,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { logout } from '../services/api'

const getUser = () => {
  try {
    return JSON.parse(localStorage.getItem('user') || '{}')
  } catch {
    return {}
  }
}

const NAV_ITEMS = [
  { to: '/dashboard',        label: 'Dashboard',          icon: LayoutDashboard },
  { to: '/transactions/new', label: 'New Transaction',     icon: PlusCircle      },
  { to: '/transactions',     label: 'Transaction History', icon: History         },
  { to: '/monitoring',       label: 'Model Monitoring',    icon: BarChart2       },
]

const navLinkCls = ({ isActive }) =>
  `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
    isActive
      ? 'bg-blue-600 text-white'
      : 'text-gray-400 hover:bg-gray-800 hover:text-gray-100'
  }`

export default function Layout() {
  const navigate = useNavigate()
  const currentUser = getUser()
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const closeSidebar = () => setSidebarOpen(false)

  const handleLogout = async () => {
    try {
      await logout()
    } catch {
      // best-effort; clear session regardless
    }
    localStorage.clear()
    toast.success('Logged out')
    navigate('/login')
  }

  return (
    <div className="flex h-screen bg-gray-950 text-gray-100 overflow-hidden">
      {/* Mobile backdrop */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black/60 z-40 lg:hidden"
          onClick={closeSidebar}
          aria-hidden="true"
        />
      )}

      {/* Sidebar */}
      <aside
        className={`
          fixed inset-y-0 left-0 z-50 w-64 bg-gray-900 border-r border-gray-800 flex flex-col
          transform transition-transform duration-200 ease-in-out
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
          lg:relative lg:w-60 lg:flex-shrink-0 lg:translate-x-0 lg:transition-none
        `}
      >
        {/* Logo */}
        <div className="flex items-center justify-between px-5 py-5 border-b border-gray-800">
          <div className="flex items-center gap-3">
            <Shield className="w-7 h-7 text-blue-500 flex-shrink-0" />
            <span className="font-bold text-lg tracking-tight">PayShield AI</span>
          </div>
          <button
            onClick={closeSidebar}
            className="lg:hidden text-gray-500 hover:text-gray-300 transition-colors p-1 rounded"
            aria-label="Close sidebar"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/transactions'}
              className={navLinkCls}
              onClick={closeSidebar}
            >
              <Icon className="w-4 h-4 flex-shrink-0" />
              {label}
            </NavLink>
          ))}

          {currentUser.role === 'ADMIN' && (
            <NavLink to="/admin" className={navLinkCls} onClick={closeSidebar}>
              <ShieldCheck className="w-4 h-4 flex-shrink-0" />
              Admin
            </NavLink>
          )}
        </nav>

        {/* User footer */}
        <div className="px-3 py-4 border-t border-gray-800">
          <div className="px-3 py-2 mb-1">
            <p className="text-xs text-gray-500 truncate">{currentUser.email}</p>
            <p className="text-xs text-blue-400 font-medium">{currentUser.role}</p>
          </div>
          <button
            onClick={handleLogout}
            className="flex items-center gap-3 w-full px-3 py-2.5 rounded-lg text-sm font-medium text-gray-400 hover:bg-gray-800 hover:text-red-400 transition-colors"
          >
            <LogOut className="w-4 h-4" />
            Sign out
          </button>
        </div>
      </aside>

      {/* Main content */}
      <div className="flex-1 flex flex-col overflow-hidden min-w-0">
        {/* Header */}
        <header className="bg-gray-900 border-b border-gray-800 px-4 py-4 flex items-center gap-3 flex-shrink-0">
          <button
            onClick={() => setSidebarOpen(true)}
            className="lg:hidden text-gray-400 hover:text-gray-100 transition-colors p-1 rounded"
            aria-label="Open sidebar"
          >
            <Menu className="w-5 h-5" />
          </button>
          <div className="flex items-center justify-between flex-1">
            <h1 className="text-sm font-semibold text-gray-400">
              PayShield AI &mdash; Fraud Detection Platform
            </h1>
            <span className="text-sm text-gray-400 hidden sm:block truncate max-w-xs">
              {currentUser.email}
            </span>
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto p-4 sm:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
