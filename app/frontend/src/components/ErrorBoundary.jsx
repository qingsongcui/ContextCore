import { t } from "../lib/i18n.jsx";
import { Component } from 'react';
import { Button } from './ui.jsx';
export class ErrorBoundary extends Component {
  state = {
    failed: false
  };
  static getDerivedStateFromError() {
    return {
      failed: true
    };
  }
  render() {
    if (this.state.failed) return <main className="empty-state" role="alert"><h1>{t("页面暂时无法显示")}</h1><p>{t("本机保存的内容不受影响。重新加载页面后再试。")}</p><Button variant="primary" onClick={() => window.location.reload()}>{t("重新加载")}</Button></main>;
    return this.props.children;
  }
}
