import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
      "@shared": path.resolve(__dirname, "src/shared"),
      "@modules": path.resolve(__dirname, "src/modules"),
    },
  },
  server: {
    port: 5173,
    allowedHosts: "all",
    fs: {
      // Allow serving files from root node_modules (hoisted monorepo deps)
      allow: [path.resolve(__dirname, ".."), "/home/utpal-bhadra/helix/node_modules"],
    },
    proxy: {
      "/api/v1/cases": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/case-types": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/queues": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/assignments": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/my": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/rules":{ 
        target: "http://localhost:8200", 
        changeOrigin: true,
      },
     "/api/v1/data-models":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/enterprise":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/realtime":{
        target: "ws://localhost:8200",
        ws: true,
        changeOrigin: true,
      },
      "/api/v1/orchestrator":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/sitemap":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/scout-ai":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/codegen":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/tenants":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/scout":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/nlp":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/process-mining":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/auth":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/admin":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/webhooks":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/analytics":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/form-submissions":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/observability":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/documents":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/email":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/compliance":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/user-directory":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/escalation-trees":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/push":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/hxnexus":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/portal-admin":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/portal":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/portals":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/access-roles":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/access-groups":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/knowledge": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/graph": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/apps": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/importer": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/analytics": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/hxbridge": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/sync": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/global": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/shield": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/fusion": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/payments": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/identity": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/esign": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/crm": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/invoices": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/comms": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/docintel": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/devconn": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/hxmigrate": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/deploy": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/hxwork": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/hxcanvas": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/hxdocs": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/intake": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/branches": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/commits": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/hxlogs": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/webhooks": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/hxstream/ws": {
        target: "ws://localhost:8200",
        ws: true,
        changeOrigin: true,
      },
      "/api/v1/hxstream":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/hxdbmanager": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/releases": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/marketplace": {
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/auth/me":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/auth/switch-context":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/sla":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/forms":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api/v1/admin":{
        target: "http://localhost:8200",
        changeOrigin: true,
      },
      "/api": {
        target: "http://localhost:8100",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },

    },
  },
});
