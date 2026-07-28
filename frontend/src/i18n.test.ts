/**
 * i18n — key parity, persistence, and the ko default that keeps every existing
 * Korean-string test meaningful.
 */

import { beforeEach, describe, expect, test } from 'vitest'
import { STRINGS, useI18n } from './i18n'

beforeEach(() => {
  localStorage.removeItem('vb.lang')
  useI18n.setState({ lang: 'ko' })
})

describe('dictionary parity', () => {
  test('en has exactly the keys ko has', () => {
    expect(Object.keys(STRINGS.en).sort()).toEqual(Object.keys(STRINGS.ko).sort())
  })

  test('every value has the same kind in both languages (string vs function)', () => {
    for (const key of Object.keys(STRINGS.ko) as Array<keyof typeof STRINGS.ko>) {
      expect(typeof STRINGS.en[key]).toBe(typeof STRINGS.ko[key])
    }
  })

  test('count/template functions return non-empty strings', () => {
    expect(STRINGS.ko.anchorCount(3)).toContain('3')
    expect(STRINGS.en.anchorCount(1)).toBe('1 anchor')
    expect(STRINGS.en.anchorCount(2)).toBe('2 anchors')
    expect(STRINGS.ko.nodeNotLoaded('x')).toContain('x')
    expect(STRINGS.en.neighborsHeading(5)).toContain('5')
  })
})

describe('store', () => {
  test('defaults to ko', () => {
    expect(useI18n.getState().lang).toBe('ko')
  })

  test('setLang switches and persists', () => {
    useI18n.getState().setLang('en')
    expect(useI18n.getState().lang).toBe('en')
    expect(localStorage.getItem('vb.lang')).toBe('en')
  })
})
