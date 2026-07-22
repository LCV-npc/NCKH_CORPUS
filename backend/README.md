# Backend - Spring Boot

## Công nghệ sử dụng
- Java 25
- Spring Boot 3.3.0
- Spring Web
- Spring Data JPA
- PostgreSQL
- Lombok

## Cấu trúc thư mục

```
backend/
├── pom.xml
└── src/
    ├── main/
    │   ├── java/com/demo/backend/
    │   │   ├── BackendApplication.java       # Entry point
    │   │   ├── controller/
    │   │   │   └── HealthController.java     # REST controllers
    │   │   ├── entity/
    │   │   │   └── ExampleEntity.java        # JPA Entities
    │   │   ├── repository/
    │   │   │   └── ExampleRepository.java    # Spring Data repositories
    │   │   └── service/
    │   │       └── ExampleService.java       # Business logic
    │   └── resources/
    │       └── application.properties        # App configuration
    └── test/
        └── java/com/demo/backend/
            └── BackendApplicationTests.java
```

## Cấu hình Database

Chỉnh sửa file `src/main/resources/application.properties`:

```properties
spring.datasource.url=jdbc:postgresql://localhost:5432/demo_db
spring.datasource.username=postgres
spring.datasource.password=your_password
```

## Chạy ứng dụng

### Yêu cầu
- Java 21+
- Maven 3.6+
- PostgreSQL đang chạy

### Lệnh chạy
```bash
./mvnw spring-boot:run
# hoặc
mvn spring-boot:run
```

Server sẽ chạy tại: http://localhost:8080

### Kiểm tra
```bash
curl http://localhost:8080/api/health
```
