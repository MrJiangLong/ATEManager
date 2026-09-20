import i18n from '../i18n'

export function applyRouteTitle(route) {
  const t = i18n.global.t
  const appName = t('app.name')
  const rawTitle = route?.meta?.titleKey ? t(route.meta.titleKey) : ''
  // 路由未声明 titleKey 时直接用 app.name，避免出现 "app.name - app.name"
  document.title = rawTitle && rawTitle !== appName ? `${rawTitle} - ${appName}` : appName
}
