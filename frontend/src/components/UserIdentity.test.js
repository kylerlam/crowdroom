import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import UserIdentity from './UserIdentity.vue';

const publicId = 'CR-4827-9163';
let wrapper;
let originalExecCommand;

beforeEach(() => {
  vi.useFakeTimers();
  originalExecCommand = Object.getOwnPropertyDescriptor(document, 'execCommand');
});
afterEach(() => {
  wrapper?.unmount();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  if (originalExecCommand) Object.defineProperty(document, 'execCommand', originalExecCommand);
  else delete document.execCommand;
  vi.useRealTimers();
  document.body.innerHTML = '';
});

function renderIdentity() {
  wrapper = mount(UserIdentity, { props: { publicId }, attachTo: document.body });
}

function setClipboard(writeText) {
  vi.stubGlobal('navigator', { clipboard: writeText ? { writeText } : undefined });
}

describe('public CrowdRoom ID', () => {
  it('copies the entire displayed ID and gives visible touch and accessible feedback', async () => {
    const writeText = vi.fn().mockResolvedValue();
    setClipboard(writeText);
    renderIdentity();
    expect(wrapper.get('.user-identity-value').text()).toBe(publicId);
    const button = wrapper.get('button');
    expect(button.attributes('data-tooltip')).toBe('Copy ID');
    expect(button.attributes('aria-label')).toBe(`Copy CrowdRoom ID ${publicId}`);
    await button.trigger('click');
    await flushPromises();
    expect(writeText).toHaveBeenCalledExactlyOnceWith(publicId);
    expect(button.attributes('data-tooltip')).toBe('Copied!');
    expect(button.classes()).toContain('identity-copy-copied');
    expect(wrapper.get('[role="status"]').text()).toBe('Copied!');
    await vi.advanceTimersByTimeAsync(2000);
    expect(button.attributes('data-tooltip')).toBe('Copy ID');
    expect(wrapper.get('[role="status"]').text()).toBe('');
  });

  it('uses a user-initiated selection copy when the Clipboard API is missing', async () => {
    setClipboard();
    const execCommand = vi.fn(() => {
      expect(document.querySelector('textarea').value).toBe(publicId);
      return true;
    });
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true });
    renderIdentity();
    const button = wrapper.get('button');
    button.element.focus();
    await button.trigger('click');
    expect(execCommand).toHaveBeenCalledExactlyOnceWith('copy');
    expect(document.querySelector('textarea')).toBeNull();
    expect(document.activeElement).toBe(button.element);
    expect(button.attributes('data-tooltip')).toBe('Copied!');
  });

  it('falls back after a denied Clipboard API request without leaving a hidden input', async () => {
    setClipboard(vi.fn().mockRejectedValue(new Error('Clipboard denied')));
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true });
    renderIdentity();
    await wrapper.get('button').trigger('click');
    await flushPromises();
    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(document.querySelector('textarea')).toBeNull();
    expect(wrapper.get('[role="status"]').text()).toBe('Copied!');
  });

  it('reports failed copying instead of claiming success, leaving the ID available to select', async () => {
    setClipboard(vi.fn().mockRejectedValue(new Error('Clipboard denied')));
    Object.defineProperty(document, 'execCommand', { value: vi.fn().mockReturnValue(false), configurable: true });
    renderIdentity();
    await wrapper.get('button').trigger('click');
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toBe('Could not copy. Select the ID and copy it manually.');
    expect(wrapper.get('.user-identity-value').text()).toBe(publicId);
    expect(wrapper.get('button').attributes('data-tooltip')).toBe('Copy ID');
    expect(wrapper.find('.identity-copy-copied').exists()).toBe(false);
    expect(document.querySelector('textarea')).toBeNull();
  });

  it('restores focus and reports an exception from the legacy copy operation', async () => {
    setClipboard();
    Object.defineProperty(document, 'execCommand', { value: vi.fn(() => { throw new Error('Denied'); }), configurable: true });
    renderIdentity();
    const button = wrapper.get('button');
    button.element.focus();
    await button.trigger('click');
    expect(document.activeElement).toBe(button.element);
    expect(document.querySelector('textarea')).toBeNull();
    expect(wrapper.find('[role="alert"]').exists()).toBe(true);
  });

  it('does not show stale success if the displayed account changes while copying', async () => {
    let finish;
    setClipboard(vi.fn(() => new Promise((resolve) => { finish = resolve; })));
    renderIdentity();
    wrapper.get('button').element.focus();
    await wrapper.get('button').trigger('click');
    expect(wrapper.get('button').attributes('aria-busy')).toBe('true');
    expect(document.activeElement).toBe(wrapper.get('button').element);
    await wrapper.setProps({ publicId: 'CR-1111-2222' });
    finish();
    await flushPromises();
    expect(wrapper.find('.identity-copy-copied').exists()).toBe(false);
    expect(wrapper.get('.user-identity-value').text()).toBe('CR-1111-2222');
  });
});
