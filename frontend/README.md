# Frontend - React + Vite

## Công nghệ sử dụng
- Node.js
- React 18
- Vite (build tool)

## Cấu trúc thư mục

```
frontend/
├── index.html
├── package.json
├── vite.config.js
├── public/
└── src/
    ├── main.jsx          # Entry point
    ├── App.jsx           # Root component
    ├── App.css
    └── assets/
```

## Cài đặt và chạy

```bash
# Cài dependencies (nếu chưa cài)
npm install

# Chạy development server
npm run dev
```

Server sẽ chạy tại: http://localhost:5173

## Build production

```bash
npm run build
```

## Kết nối với Backend

Để gọi API tới backend Spring Boot (http://localhost:8080), thêm vào `vite.config.js`:

```js
server: {
  proxy: {
    '/api': {
      target: 'http://localhost:8080',
      changeOrigin: true,
    }
  }
}
```
