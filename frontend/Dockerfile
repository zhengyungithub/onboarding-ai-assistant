# 前端 Dockerfile - 多阶段构建
# 阶段1：构建
FROM node:18-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm config set cache /app/.npm-cache && npm install --registry=https://registry.npmmirror.com
COPY . .
RUN npm run build

# 阶段2：Nginx 托管
FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
