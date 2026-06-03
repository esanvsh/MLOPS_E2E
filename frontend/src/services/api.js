import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL,
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.clear()
      window.location.href = '/login'
    }
    return Promise.reject(error)
  },
)

export const register = (email, password) =>
  api.post('/register', { email, password })

export const login = (email, password) =>
  api.post('/login', { email, password })

export const logout = () =>
  api.post('/logout')

export const createTransaction = (data) =>
  api.post('/transactions', data)

export const getTransactions = () =>
  api.get('/transactions')

export const getTransaction = (id) =>
  api.get(`/transactions/${id}`)

export default api
