import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// jsdom 未实现滚动相关 API，真实浏览器有。缺了它，任何调用
// element.scrollTo(...) 的 effect 在测试中都会抛
// "scrollTo is not a function"，导致整棵组件树渲染失败。
// 这里按 jsdom 的既有行为补一个空实现：jsdom 不做布局，
// scrollTop/scrollHeight 恒为 0，滚动本就无副作用可观测。
if (typeof Element !== 'undefined' && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollTo(): void {
    // no-op：jsdom 无布局引擎，滚动位置不可测
  }
}
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView(): void {
    // no-op：同上
  }
}

// 每个用例结束后卸载 React 树，避免用例间 DOM 残留。
afterEach(() => {
  cleanup()
})
