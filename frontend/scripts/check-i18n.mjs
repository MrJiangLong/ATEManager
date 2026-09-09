// 临时校验脚本：检查 i18n key 定义与使用情况（校验后删除）
import fs from 'fs'
import path from 'path'
import zh from '../src/locales/zh-CN.js'
import en from '../src/locales/en-US.js'

function flat(d, p = '') {
  const out = new Set()
  for (const [k, v] of Object.entries(d)) {
    const kk = p ? `${p}.${k}` : k
    if (v && typeof v === 'object') flat(v, kk).forEach((x) => out.add(x))
    else out.add(kk)
  }
  return out
}
const zhK = flat(zh)
const enK = flat(en)
console.log(`zh keys: ${zhK.size} | en keys: ${enK.size}`)
console.log('MISSING in en:', [...zhK].filter((k) => !enK.has(k)))
console.log('MISSING in zh:', [...enK].filter((k) => !zhK.has(k)))

const root = path.resolve('src')
const used = new Map()
function walk(dir) {
  for (const f of fs.readdirSync(dir, { withFileTypes: true })) {
    const fp = path.join(dir, f.name)
    if (f.isDirectory()) walk(fp)
    else if (/\.(vue|js)$/.test(f.name)) {
      const t = fs.readFileSync(fp, 'utf8')
      const pats = [
        /\$t\(\s*['"]([a-zA-Z0-9_.]+)['"]/g,
        /\bt\(\s*['"]([a-zA-Z0-9_.]+)['"]/g,
        /labelKey:\s*['"]([a-zA-Z0-9_.]+)['"]/g,
        /titleKey:\s*['"]([a-zA-Z0-9_.]+)['"]/g,
      ]
      for (const re of pats) {
        let m
        while ((m = re.exec(t))) {
          if (!used.has(m[1])) used.set(m[1], [])
          used.get(m[1]).push(path.relative('src', fp))
        }
      }
    }
  }
}
walk(root)

const missing = [...used.keys()].filter((k) => !zhK.has(k))
console.log(`\nUSED BUT UNDEFINED (${missing.length}):`)
for (const k of missing.sort()) console.log(`   ${k}  ->  ${used.get(k)[0]}`)
