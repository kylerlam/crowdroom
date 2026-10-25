import { defineComponent } from 'vue';
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ADMISSION_MESSAGE, useRoomAdmission } from './useRoomAdmission.js';

const mocks = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn(), push: vi.fn() }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock('./useApi.js', () => ({ apiGet: mocks.apiGet, apiPost: mocks.apiPost }));

let wrapper;
const pending = { room_id: 'room', room_name: 'Music room', status: 'pending' };

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  mocks.apiPost.mockResolvedValue(pending);
  mocks.apiGet.mockResolvedValue({ status: 'pending' });
  wrapper = mount(defineComponent({ setup: useRoomAdmission, template: '<div />' }));
});
afterEach(() => {
  wrapper.unmount();
  vi.useRealTimers();
});

describe('first room admission', () => {
  it('shows approval instructions only after a successful validated join request', async () => {
    expect(wrapper.vm.admission).toBeNull();
    await wrapper.vm.join('room', '/rooms/room/join', { pin: '1234' });
    expect(wrapper.vm.admission.message).toBe(ADMISSION_MESSAGE);
    expect(mocks.push).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(10000);
    expect(mocks.apiPost).toHaveBeenCalledTimes(1);
    expect(mocks.apiGet).toHaveBeenCalledWith('/rooms/room/admission/status');
  });

  it('retains the original PIN and only enters after approval and a successful second join', async () => {
    const body = { pin: '1234' };
    await wrapper.vm.join('room', '/rooms/room/join', body);
    body.pin = '';
    mocks.apiGet.mockResolvedValue({ status: 'approved' });
    mocks.apiPost.mockResolvedValue({ room_id: 'room', status: 'joined' });
    await wrapper.vm.checkStatus();
    expect(mocks.apiPost).toHaveBeenNthCalledWith(2, '/rooms/room/join', { pin: '1234' });
    expect(mocks.push).toHaveBeenCalledWith('/room/room');
    expect(wrapper.vm.admission).toBeNull();
  });

  it('retains an invitation while polling status without consuming it again', async () => {
    await wrapper.vm.join('room', '/rooms/invite/private-token/join');
    await vi.advanceTimersByTimeAsync(5000);
    expect(mocks.apiPost).toHaveBeenCalledTimes(1);
    mocks.apiGet.mockResolvedValue({ status: 'approved' });
    mocks.apiPost.mockResolvedValue({ room_id: 'room', status: 'joined' });
    await wrapper.vm.checkStatus();
    expect(mocks.apiPost).toHaveBeenNthCalledWith(2, '/rooms/invite/private-token/join', {});
  });

  it('does not show a new-user prompt for invalid PINs or banned users', async () => {
    mocks.apiPost.mockRejectedValue({ status: 403, detail: 'Invalid PIN' });
    await expect(
      wrapper.vm.join('room', '/rooms/room/join', { pin: 'wrong' }),
    ).rejects.toMatchObject({ status: 403 });
    expect(wrapper.vm.admission).toBeNull();
    await vi.advanceTimersByTimeAsync(10000);
    expect(mocks.apiGet).not.toHaveBeenCalled();
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it('stops automatic requests after rejection without admitting the user', async () => {
    await wrapper.vm.join('room', '/rooms/room/join');
    mocks.apiGet.mockResolvedValue({ status: 'rejected' });
    await wrapper.vm.checkStatus();
    expect(wrapper.vm.admission.status).toBe('rejected');
    expect(wrapper.vm.admission.message).toContain('Please contact the room host');
    const count = mocks.apiGet.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10000);
    expect(mocks.apiGet).toHaveBeenCalledTimes(count);
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it('keeps the user outside when the approved invitation has expired', async () => {
    await wrapper.vm.join('room', '/rooms/invite/token/join');
    mocks.apiGet.mockResolvedValue({ status: 'approved' });
    mocks.apiPost.mockRejectedValue({ status: 410, detail: 'Invite expired' });
    await wrapper.vm.checkStatus();
    expect(wrapper.vm.error).toBe('Invite expired');
    expect(mocks.push).not.toHaveBeenCalled();
    expect(wrapper.vm.admission.status).toBe('none');
  });

  it('stops polling on cancellation and unmount', async () => {
    await wrapper.vm.join('room', '/rooms/room/join');
    wrapper.vm.cancel();
    await vi.advanceTimersByTimeAsync(5000);
    await flushPromises();
    expect(mocks.apiGet).not.toHaveBeenCalled();
    expect(wrapper.vm.admission).toBeNull();
  });
});
