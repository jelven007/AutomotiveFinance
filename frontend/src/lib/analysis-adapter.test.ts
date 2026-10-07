import { describe, expect, it } from 'vitest'

import { pickBestDimensionConfig } from './analysis-adapter'

const configs = [
  {
    id: 'ext_gn_ths',
    label: '扩展概念',
    description: '概念和行业页面可用',
    fields: [{ name: '所属概念', label: '所属概念' }],
  },
  {
    id: 'ext_hy_ths',
    label: '扩展行业',
    fields: [{ name: '所属同花顺行业', label: '所属同花顺行业' }],
  },
]

describe('pickBestDimensionConfig', () => {
  it('prefers the matching built-in preset over keyword ties', () => {
    expect(pickBestDimensionConfig(configs, 'ext_hy_ths', ['industry', '行业', 'sector']))
      .toBe('ext_hy_ths')
    expect(pickBestDimensionConfig(configs, 'ext_gn_ths', ['concept', '概念', 'theme']))
      .toBe('ext_gn_ths')
  })

  it('falls back to keyword scoring when the preset is unavailable', () => {
    expect(pickBestDimensionConfig(
      configs.filter(config => config.id !== 'ext_hy_ths'),
      'ext_hy_ths',
      ['concept', '概念'],
    )).toBe('ext_gn_ths')
  })
})
