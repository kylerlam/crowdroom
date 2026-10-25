import { ref, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import { apiGet, apiPost } from './useApi.js';

export const ADMISSION_MESSAGE =
  'Your request to join has been sent. Please contact the room host or an administrator for approval.';

export function useRoomAdmission() {
  const router = useRouter();
  const admission = ref(null);
  const busy = ref(false);
  const error = ref('');
  let target = null;
  let timer = null;

  function scheduleCheck() {
    clearTimeout(timer);
    if (target && admission.value?.status === 'pending') {
      timer = setTimeout(checkStatus, 5000);
    }
  }

  function cancel() {
    target = null;
    admission.value = null;
    error.value = '';
    clearTimeout(timer);
  }

  async function submit(context) {
    busy.value = true;
    error.value = '';
    try {
      const result = await apiPost(context.path, context.body);
      if (!result) return;
      if (target !== context) return result;
      if (result.status === 'pending' || result.status === 'rejected') {
        context.roomId = result.room_id;
        admission.value = {
          ...result,
          message:
            result.message ||
            (result.status === 'pending'
              ? ADMISSION_MESSAGE
              : 'Your request was not approved. Please contact the room host or an administrator.'),
        };
        scheduleCheck();
      } else {
        cancel();
        await router.push(`/room/${result.room_id || context.roomId}`);
      }
      return result;
    } finally {
      busy.value = false;
    }
  }

  async function join(roomId, path, body = {}) {
    cancel();
    const context = { roomId, path, body: { ...body } };
    target = context;
    try {
      return await submit(context);
    } catch (err) {
      if (target === context) cancel();
      throw err;
    }
  }

  async function checkStatus() {
    if (!target || busy.value) return;
    clearTimeout(timer);
    const context = target;
    busy.value = true;
    error.value = '';
    let approved = false;
    try {
      const result = await apiGet(`/rooms/${context.roomId}/admission/status`);
      if (target !== context) return;
      approved = result.status === 'approved';
      if (result.status === 'rejected') {
        admission.value = {
          ...admission.value,
          status: 'rejected',
          message:
            'Your request was not approved. Please contact the room host or an administrator.',
        };
      } else if (result.status === 'none') {
        admission.value = {
          ...admission.value,
          status: 'none',
          message:
            'Your request is no longer pending. Please contact the room host or an administrator.',
        };
      }
    } catch (err) {
      if (target === context) {
        error.value = err.detail || 'Could not check approval. Try again.';
        if ([403, 404, 410].includes(err.status)) {
          admission.value = { ...admission.value, status: 'none' };
        }
      }
    } finally {
      busy.value = false;
    }
    if (approved && target === context) {
      try {
        await submit(context);
      } catch (err) {
        if (target === context) {
          error.value = err.detail || 'Could not enter the room. Try joining again.';
          admission.value = { ...admission.value, status: 'none' };
        }
      }
    }
    scheduleCheck();
  }

  onUnmounted(cancel);
  return { admission, busy, error, join, checkStatus, cancel };
}
