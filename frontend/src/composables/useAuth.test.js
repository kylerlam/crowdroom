import { afterEach, describe, expect, it, vi } from 'vitest';
import { clearAuth, fetchMe, useAuth } from './useAuth.js';

afterEach(() => {
  clearAuth();
  vi.unstubAllGlobals();
});

describe('public account identity', () => {
  it('loads the server-issued ID from the same authenticated identity used after login', async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: 'uuid', username: 'User', public_id: 'CR-4827-9163' }),
    });
    vi.stubGlobal('fetch', fetch);
    const auth = useAuth();
    await fetchMe();
    expect(fetch).toHaveBeenCalledWith('/api/v1/auth/me', { credentials: 'include' });
    expect(auth.publicId.value).toBe('CR-4827-9163');
    expect(auth.userId.value).toBe('uuid');
    expect(auth.isAuthenticated.value).toBe(true);
  });

  it('clears the public ID on logout or an expired session instead of displaying the prior user', async () => {
    const fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: 'uuid', public_id: 'CR-4827-9163' }),
    }).mockResolvedValue({ ok: false });
    vi.stubGlobal('fetch', fetch);
    const auth = useAuth();
    await fetchMe();
    await auth.logout();
    expect(auth.publicId.value).toBe('');
    expect(auth.userId.value).toBeNull();
    expect(fetch).toHaveBeenLastCalledWith('/api/v1/auth/logout', { method: 'POST', credentials: 'include' });
    await fetchMe();
    expect(auth.publicId.value).toBe('');
  });
});
