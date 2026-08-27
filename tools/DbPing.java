import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;

/**
 * JDBC 连通性测试：输出一行 JSON，供 FastAPI 子进程解析。
 * 用法：java -cp "tools;<驱动jar目录>/*" DbPing <jdbc-url> <user> <password> <driverClass>
 * SELECT 1 FROM DUAL：MySQL 与达梦(DM8, Oracle兼容) 都支持。
 */
public class DbPing {
    public static void main(String[] args) {
        if (args.length < 4) {
            System.out.println("{\"ok\":false,\"error\":\"usage: DbPing <url> <user> <pass> <driverClass>\"}");
            return;
        }
        long t0 = System.currentTimeMillis();
        try {
            DriverManager.setLoginTimeout(5);
            Class.forName(args[3]);
            try (Connection c = DriverManager.getConnection(args[0], args[1], args[2]);
                 Statement s = c.createStatement()) {
                s.setQueryTimeout(5);
                try (ResultSet r = s.executeQuery("SELECT 1 FROM DUAL")) {
                    r.next();
                }
            }
            System.out.println("{\"ok\":true,\"ms\":" + (System.currentTimeMillis() - t0) + "}");
        } catch (Exception e) {
            String msg = e.getMessage() == null ? e.getClass().getName() : e.getMessage();
            msg = msg.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ").replace("\r", " ");
            System.out.println("{\"ok\":false,\"ms\":" + (System.currentTimeMillis() - t0) + ",\"error\":\"" + msg + "\"}");
        }
    }
}
