import { http } from './http'
import { setToken } from '../auth/token'

export type UserMe = {
  id: string
  email: string
  username: string
}

/** 与后端 AuthTokenResponse 对齐：{ access_token, user } */
type AuthTokenPayload = {
  access_token: string
  user: UserMe
}

export async function fetchMe(): Promise<UserMe> {
  const r = await http.get('/users/me')
  return r.data.data as UserMe
}

export async function login(email: string, password: string): Promise<UserMe> {
  const r = await http.post('/auth/login', { email, password })
  const payload = r.data.data as AuthTokenPayload
  if (payload?.access_token) setToken(payload.access_token)
  return payload.user as UserMe
}

export async function register(
  email: string,
  username: string,
  password: string,
): Promise<UserMe> {
  const r = await http.post('/auth/register', { email, username, password })
  const payload = r.data.data as AuthTokenPayload
  if (payload?.access_token) setToken(payload.access_token)
  return payload.user as UserMe
}

export async function logout(): Promise<void> {
  await http.post('/auth/logout')
}
