import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ConnectionPage from './ConnectionPage.vue';

const mocks = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn(), apiDelete: vi.fn() }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('../components/ThemeToggle.vue', () => ({ default: { template: '<span />' } }));
vi.mock('../composables/useToast.js', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('../composables/useApi.js', () => ({
  apiGet: mocks.apiGet,
  apiPost: mocks.apiPost,
  apiDelete: mocks.apiDelete,
}));

let wrapper;
let connections;
let canManage;
beforeEach(() => {
  vi.clearAllMocks();
  canManage = true;
  sessionStorage.clear();
  connections = [
    { platform: 'spotify', connection_type: 'client_credentials', token_expires_at: null },
  ];
  mocks.apiGet.mockImplementation(async (path) => {
    if (path === '/platform-connections/spotify/access') return { can_manage: canManage };
    return path === '/platform-connections/' ? connections : { has_credentials: true };
  });
});
afterEach(() => wrapper?.unmount());

async function renderConnection() {
  wrapper = mount(ConnectionPage);
  await flushPromises();
}

describe('Spotify connection page', () => {
  it('does not load or expose private connections when the server denies management', async () => {
    canManage = false;
    await renderConnection();
    expect(wrapper.text()).toContain('Only the room host can manage Spotify.');
    expect(wrapper.find('.connections-list').exists()).toBe(false);
    expect(mocks.apiGet).toHaveBeenCalledWith('/platform-connections/spotify/access');
    expect(mocks.apiGet).not.toHaveBeenCalledWith('/platform-connections/');
    expect(mocks.apiGet).not.toHaveBeenCalledWith(
      '/platform-connections/spotify/has-app-credentials',
    );
  });

  it('hides management if the user enters another host room while this page is open', async () => {
    await renderConnection();
    canManage = false;
    window.dispatchEvent(new Event('focus'));
    await flushPromises();
    expect(wrapper.find('.connections-list').exists()).toBe(false);
    expect(wrapper.findAll('button').some((button) => button.text() === 'Connect Spotify')).toBe(
      false,
    );
  });

  it('hides controls after a server permission rejection without leaving a stale OAuth return', async () => {
    await renderConnection();
    mocks.apiPost.mockRejectedValue({
      status: 403,
      detail: 'Only the room host can manage Spotify.',
    });
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Connect Spotify')
      .trigger('click');
    await flushPromises();
    expect(sessionStorage.getItem('spotify_return_page')).toBeNull();
    expect(wrapper.find('.connections-list').exists()).toBe(false);
  });

  it('starts account authorization after saving app credentials', async () => {
    mocks.apiGet.mockImplementation(async (path) => {
      if (path === '/platform-connections/spotify/access') return { can_manage: true };
      return path === '/platform-connections/' ? [] : { has_credentials: false };
    });
    mocks.apiPost.mockResolvedValue({ authorize_url: '#spotify-authorize' });
    await renderConnection();
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Set up')
      .trigger('click');
    await wrapper.get('#spotify-client-id').setValue('test-app-id');
    await wrapper.get('#spotify-client-secret').setValue('test-app-secret');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledWith('/auth/spotify/start');
    expect(sessionStorage.getItem('spotify_return_page')).toBe('/connection');
  });

  it('does not label app credentials as a connected playback account', async () => {
    await renderConnection();
    expect(wrapper.find('.connection-status.connected').exists()).toBe(false);
    expect(wrapper.text()).toContain('Not connected');
    expect(wrapper.text()).toContain('Authorize your Spotify account');
    expect(wrapper.findAll('button').some((button) => button.text() === 'Connect Spotify')).toBe(
      true,
    );
  });

  it('recognizes OAuth with an expired access token as refreshable', async () => {
    connections = [
      {
        platform: 'spotify',
        connection_type: 'authorization_code',
        token_expires_at: '2020-01-01T00:00:00',
      },
    ];
    await renderConnection();
    expect(wrapper.get('.connection-status.connected').text()).toBe('Connected');
  });

  it('requires authorization when an OAuth row has no issued token', async () => {
    connections = [
      { platform: 'spotify', connection_type: 'authorization_code', token_expires_at: null },
    ];
    await renderConnection();
    expect(wrapper.find('.connection-status.connected').exists()).toBe(false);
  });

  it('keeps configured app credentials usable after disconnecting an account', async () => {
    connections = [
      {
        platform: 'spotify',
        connection_type: 'authorization_code',
        token_expires_at: '2099-01-01T00:00:00',
      },
    ];
    await renderConnection();
    mocks.apiDelete.mockImplementation(async () => {
      connections = [];
    });
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Disconnect')
      .trigger('click');
    await flushPromises();
    expect(wrapper.findAll('button').some((button) => button.text() === 'Connect Spotify')).toBe(
      true,
    );
  });
});
