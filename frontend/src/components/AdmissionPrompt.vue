<template>
  <section class="panel admission-prompt" role="status" aria-live="polite">
    <h3>{{ request.status === 'pending' ? 'Waiting for approval' : 'Contact the room host' }}</h3>
    <p v-if="request.room_name" class="text-secondary">{{ request.room_name }}</p>
    <p>{{ request.message }}</p>
    <UserIdentity :public-id="request.public_id" />
    <p v-if="error" class="error-text">{{ error }}</p>
    <div class="admission-actions">
      <button class="btn btn-secondary" :disabled="busy" @click="$emit('check')">
        {{ busy ? 'Checking…' : 'Check status' }}
      </button>
      <button class="btn btn-ghost" @click="$emit('cancel')">Dismiss</button>
    </div>
  </section>
</template>

<script setup>
import UserIdentity from './UserIdentity.vue';

defineProps({
  request: { type: Object, required: true },
  busy: { type: Boolean, default: false },
  error: { type: String, default: '' },
});
defineEmits(['check', 'cancel']);
</script>

<style scoped>
.admission-prompt {
  margin-block: var(--space-4);
}
.admission-prompt p {
  margin-top: var(--space-3);
}
.admission-prompt .user-identity {
  margin-top: var(--space-3);
}
.admission-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-4);
}
</style>
