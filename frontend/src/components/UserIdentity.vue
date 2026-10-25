<template>
  <div v-if="publicId" class="user-identity">
    <span class="user-identity-label">CrowdRoom ID</span>
    <span class="user-identity-content">
      <code class="user-identity-value">{{ publicId }}</code>
      <button
        type="button"
        class="identity-copy"
        :class="{ 'identity-copy-copied': copied }"
        :aria-label="`Copy CrowdRoom ID ${publicId}`"
        :data-tooltip="copied ? 'Copied!' : 'Copy ID'"
        :aria-busy="copying"
        @click.stop="copyId"
      >
        <i :class="copied ? 'ph ph-check' : 'ph ph-copy'" aria-hidden="true"></i>
      </button>
    </span>
    <span class="identity-copy-status" role="status" aria-live="polite">{{ copied ? 'Copied!' : '' }}</span>
    <span v-if="copyError" class="identity-copy-error error-text" role="alert">{{ copyError }}</span>
  </div>
</template>

<script setup>
import { ref, watch, onUnmounted } from 'vue';

const props = defineProps({ publicId: { type: String, default: '' } });
const copied = ref(false);
const copying = ref(false);
const copyError = ref('');
let resetTimer;
let active = true;

function resetFeedback() {
  clearTimeout(resetTimer);
  copied.value = false;
  copyError.value = '';
}

function copyWithSelection(value) {
  const previousFocus = document.activeElement;
  const input = document.createElement('textarea');
  input.value = value;
  input.setAttribute('readonly', '');
  input.setAttribute('aria-hidden', 'true');
  input.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;';
  document.body.appendChild(input);
  try {
    input.select();
    return document.execCommand?.('copy') === true;
  } finally {
    input.remove();
    previousFocus?.focus({ preventScroll: true });
  }
}

async function copyId() {
  if (copying.value || !props.publicId) return;
  const value = props.publicId;
  resetFeedback();
  copying.value = true;
  try {
    let success = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        success = true;
      } catch {
        // A browser may deny Clipboard API access while allowing a user-initiated copy.
      }
    }
    if (!success) success = copyWithSelection(value);
    if (!success) throw new Error('Copy unavailable');
    if (!active || props.publicId !== value) return;
    copied.value = true;
    resetTimer = setTimeout(resetFeedback, 2000);
  } catch {
    if (active && props.publicId === value) {
      copyError.value = 'Could not copy. Select the ID and copy it manually.';
    }
  } finally {
    copying.value = false;
  }
}

watch(() => props.publicId, resetFeedback);
onUnmounted(() => {
  active = false;
  clearTimeout(resetTimer);
});
</script>

<style scoped>
.user-identity {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-1) var(--space-2);
  min-width: 0;
  max-width: 100%;
}
.user-identity-label {
  color: var(--text-secondary);
  font-size: var(--font-size-xs);
}
.user-identity-content {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
}
.user-identity-value {
  color: var(--text-primary);
  font-size: var(--font-size-sm);
  white-space: nowrap;
  user-select: text;
}
.identity-copy {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  background: none;
  cursor: pointer;
}
.identity-copy:hover,
.identity-copy-copied {
  color: var(--accent);
  border-color: var(--accent);
}
.identity-copy:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.identity-copy::after {
  content: attr(data-tooltip);
  position: absolute;
  bottom: calc(100% + var(--space-2));
  left: 50%;
  transform: translateX(-50%);
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  color: var(--text-primary);
  font-size: var(--font-size-xs);
  font-weight: var(--font-weight-regular);
  line-height: 1.5;
  white-space: nowrap;
  opacity: 0;
  pointer-events: none;
  z-index: 60;
}
.identity-copy:hover::after,
.identity-copy:focus-visible::after,
.identity-copy-copied::after {
  opacity: 1;
}
.identity-copy-error {
  flex-basis: 100%;
  font-size: var(--font-size-xs);
}
.identity-copy-status {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
</style>
