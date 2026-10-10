// 板块判断工具函数

export const BOARDS = ['沪主板', '深主板', '创业板', '科创板'] as const
export type BoardType = (typeof BOARDS)[number]

/** 根据股票代码判断板块 */
export function getBoardType(symbol: string): BoardType | null {
  const [code, exchange] = symbol.toUpperCase().split('.')
  if (exchange && !['SH', 'SZ'].includes(exchange)) return null
  if (exchange === 'SH' && !/^(600|601|603|605|688|689)\d{3}$/.test(code)) return null
  if (exchange === 'SZ' && !/^(000|001|002|003|300|301)\d{3}$/.test(code)) return null
  if (/^(300|301)/.test(symbol)) return '创业板'
  if (/^(688|689)/.test(symbol)) return '科创板'
  if (/^60[0135]/.test(symbol)) return '沪主板'
  if (/^00[0123]/.test(symbol)) return '深主板'
  return null
}

/** 板块简称标签: 主板返回空字符串(不显示), 创/科 等返回简称 */
export function boardTag(symbol: string): string {
  const b = getBoardType(symbol)
  if (!b) return ''
  if (b === '沪主板' || b === '深主板') return ''
  if (b === '创业板') return '创'
  if (b === '科创板') return '科'
  return ''
}
