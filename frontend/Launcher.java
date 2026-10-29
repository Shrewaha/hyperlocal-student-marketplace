/*
 * Entry point for the packaged build.
 *
 * MarketplaceApp extends javafx.application.Application, and the JVM refuses
 * to start such a class directly unless JavaFX is on the module path:
 *
 *     Error: JavaFX runtime components are missing, and are required to run
 *
 * The check only looks at the class named in Main-Class. A plain class that
 * calls MarketplaceApp.main() sidesteps it, which is what lets the shaded jar
 * run with a bare `java -jar MarketplaceApp.jar` on a machine that has never
 * heard of the JavaFX SDK.
 *
 * Running MarketplaceApp straight from the IDE still works exactly as before;
 * this class is only involved in the packaged build.
 */
public final class Launcher {

    private Launcher() {
        // Not meant to be instantiated -- this is a launcher, not an object.
    }

    public static void main(String[] args) {
        MarketplaceApp.main(args);
    }
}
